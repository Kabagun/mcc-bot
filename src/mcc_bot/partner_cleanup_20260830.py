"""Reconcile the first partner snapshot with the reviewed MCC-bound catalog.

This release-time migration is intentionally separate from the insert-missing
seed loader.  It only rewrites rows that still exactly match the superseded
2026-08-30 snapshot, so later moderator edits are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from .partner_rewards import (
    PartnerOffer,
    PartnerOfferInput,
    PartnerRepository,
    PartnerRewardError,
    PartnerTierInput,
)
from .partner_seed_20260830 import SEED_PATH, _date, _tier, load_seed
from .stores import StoreError, StoreRepository, normalize_store_name

REMOVED_CASHALOT = {
    "cashalot:7-karat": ("2", "https://cashalot.by/stores/store_select/7-karat/"),
    "cashalot:xistore-by": ("3", "https://cashalot.by/stores/store_select/xistore-by/"),
}
_CASHALOT_CARD = "belgazprombank_cashalot"
_STATUS_CARD = "statusbank_statuskarta"
_CASHALOT_CONDITION = "Повышенный кэшбэк в партнёрской сети Cashalot."

# These are reviewed cross-channel networks from the production-derived local
# snapshot.  The exact combined active channel/MCC set is part of the approval:
# a same-named group with any other fact is deliberately left untouched.
_REVIEWED_NETWORK_FACT_SIGNATURES = {
    "Белгосстрах": frozenset({("offline", "6300"), ("online", "6300")}),
    "Burger King": frozenset({("offline", "5812"), ("online", "5814")}),
    "Белоруснефть": frozenset({("offline", "5541"), ("offline", "5542"), ("online", "5542")}),
    "5 Элемент": frozenset({("offline", "5732"), ("online", "5732")}),
    "DODO Pizza": frozenset({("offline", "5812"), ("online", "5812")}),
    "Domino\N{RIGHT SINGLE QUOTATION MARK}s Pizza": frozenset(
        {("offline", "5812"), ("online", "5814")}
    ),
    "Papa John\N{RIGHT SINGLE QUOTATION MARK}s": frozenset(
        {("offline", "5812"), ("online", "5812")}
    ),
    "Helix": frozenset({("offline", "8071"), ("online", "8071")}),
    "Суши Весла": frozenset({("offline", "5812"), ("online", "5814")}),
    "ЧИП и ДИП": frozenset({("offline", "5732"), ("online", "5732")}),
    "Autolight Express": frozenset({("offline", "4214"), ("offline", "4215"), ("online", "4215")}),
}


class PartnerCleanupError(RuntimeError):
    """Raised when a legacy row was edited and cannot be reconciled safely."""


def _input(item: dict, brand_id: int) -> PartnerOfferInput:
    return PartnerOfferInput(
        brand_id=brand_id,
        card_id=item["card_id"],
        channel=item["channel"],
        mode=item["mode"],
        reward_kind=item["reward_kind"],
        tiers=tuple(_tier(tier) for tier in item["tiers"]),
        starts_on=_date(item.get("starts_on")),
        ends_on=_date(item.get("ends_on")),
        conditions=item.get("conditions", ""),
        source_url=item.get("source_url", ""),
    )


def _offer_signature(offer: PartnerOffer) -> tuple:
    return (
        offer.card_id,
        offer.channel,
        offer.mode,
        offer.reward_kind,
        offer.starts_on,
        offer.ends_on,
        offer.conditions,
        offer.source_url,
        tuple(
            (
                tier.value,
                tier.min_purchase,
                tier.max_purchase,
                tier.per_transaction_cap,
                tier.starts_on,
                tier.ends_on,
            )
            for tier in offer.tiers
        ),
    )


def _input_signature(payload: PartnerOfferInput) -> tuple:
    return (
        payload.card_id,
        payload.channel,
        payload.mode,
        payload.reward_kind,
        payload.starts_on,
        payload.ends_on,
        payload.conditions.strip(),
        payload.source_url.strip(),
        tuple(
            (
                tier.value,
                tier.min_purchase,
                tier.max_purchase,
                tier.per_transaction_cap,
                tier.starts_on,
                tier.ends_on,
            )
            for tier in payload.tiers
        ),
    )


def _legacy_removed(offer: PartnerOffer, value: str, source_url: str) -> PartnerOfferInput:
    return PartnerOfferInput(
        brand_id=offer.brand_id,
        card_id=_CASHALOT_CARD,
        channel="any",
        mode="total",
        reward_kind="cash",
        tiers=(PartnerTierInput(Decimal(value)),),
        conditions=_CASHALOT_CONDITION,
        source_url=source_url,
    )


def _active_brand_ids(connection, *, name: str, channel: str, mcc: str) -> list[int]:
    key = normalize_store_name(name)
    rows = connection.execute(
        """SELECT DISTINCT b.id,b.name,b.aliases_json
        FROM store_brands b
        JOIN store_brand_members bm ON bm.brand_id=b.id
        JOIN store_merchants m ON m.id=bm.merchant_id
        JOIN store_facts f ON f.merchant_id=m.id
        WHERE b.archived=0 AND m.archived=0 AND f.archived=0
          AND m.channel=? AND f.mcc=? ORDER BY b.id""",
        (channel, mcc),
    ).fetchall()
    return [
        row["id"]
        for row in rows
        if key
        in {
            normalize_store_name(row["name"]),
            *(normalize_store_name(alias) for alias in json.loads(row["aliases_json"])),
        }
    ]


def _active_primary_brand_ids(connection, name: str) -> list[int]:
    key = normalize_store_name(name)
    return [
        row["id"]
        for row in connection.execute(
            "SELECT id,name FROM store_brands WHERE archived=0 ORDER BY id"
        ).fetchall()
        if normalize_store_name(row["name"]) == key
    ]


def _active_exact_brands(connection, name: str) -> list:
    key = normalize_store_name(name)
    rows = connection.execute(
        """SELECT b.id,b.name,
          CASE WHEN EXISTS(SELECT 1 FROM partner_offers o WHERE o.brand_id=b.id)
            OR EXISTS(SELECT 1 FROM partner_exclusions e WHERE e.brand_id=b.id)
            OR EXISTS(SELECT 1 FROM partner_seed_brands s WHERE s.brand_id=b.id)
          THEN 1 ELSE 0 END AS partner_bound
        FROM store_brands b WHERE b.archived=0 ORDER BY b.id"""
    ).fetchall()
    return [row for row in rows if normalize_store_name(row["name"]) == key]


def _active_fact_signature(connection, brand_ids: list[int]) -> frozenset[tuple[str, str]]:
    placeholders = ",".join("?" for _ in brand_ids)
    rows = connection.execute(
        f"""SELECT DISTINCT m.channel,f.mcc
        FROM store_brand_members bm
        JOIN store_merchants m ON m.id=bm.merchant_id
        JOIN store_facts f ON f.merchant_id=m.id
        WHERE bm.brand_id IN ({placeholders}) AND m.archived=0 AND f.archived=0""",
        brand_ids,
    ).fetchall()
    return frozenset((row["channel"], row["mcc"]) for row in rows)


def _merge_reviewed_cross_channel_networks(
    stores: StoreRepository, connection, *, actor_id: int
) -> tuple[int, int]:
    applied = existing = 0
    for name, allowed_signature in _REVIEWED_NETWORK_FACT_SIGNATURES.items():
        candidates = _active_exact_brands(connection, name)
        if not candidates:
            continue
        brand_ids = [row["id"] for row in candidates]
        if _active_fact_signature(connection, brand_ids) != allowed_signature:
            continue
        if len(candidates) == 1:
            existing += 1
            continue
        # Exact canonical spelling is preferred; an existing partner binding
        # breaks ties so the most meaningful durable brand ID survives.
        target = min(
            candidates,
            key=lambda row: (
                row["name"] != name,
                not bool(row["partner_bound"]),
                row["id"],
            ),
        )
        for source in candidates:
            if source["id"] == target["id"]:
                continue
            stores.apply_change(
                "merge_brand",
                {"brand_id": source["id"], "target_id": target["id"]},
                actor_id,
                connection=connection,
            )
            applied += 1
    return applied, existing


def _merge_reviewed_duplicates(
    stores: StoreRepository, connection, *, actor_id: int
) -> tuple[int, int]:
    applied = existing = 0
    canonical_21vek = _active_primary_brand_ids(connection, "21vek")
    if len(canonical_21vek) > 1:
        raise PartnerCleanupError("Найдено несколько канонических магазинов 21vek")
    sources = _active_brand_ids(connection, name="21vek.by", channel="online", mcc="5300")
    sources.extend(
        brand_id
        for brand_id in _active_brand_ids(
            connection, name="21vek.by", channel="offline", mcc="5300"
        )
        if brand_id not in sources
    )
    if canonical_21vek or sources:
        target_id = canonical_21vek[0] if canonical_21vek else sources[0]
        merge_count = 0
        for source_id in sources:
            if source_id == target_id:
                continue
            stores.apply_change(
                "merge_brand",
                {"brand_id": source_id, "target_id": target_id},
                actor_id,
                connection=connection,
            )
            applied += 1
            merge_count += 1
        brand = stores.get_brand(target_id, connection=connection)
        if brand is None:
            raise PartnerCleanupError("Канонический магазин 21vek исчез")
        aliases = tuple(dict.fromkeys((*brand.aliases, "21vek.by", "21 век")))
        if aliases != brand.aliases:
            stores.apply_change(
                "edit_brand_names",
                {"brand_id": target_id, "name": "21vek", "aliases": aliases},
                actor_id,
                connection=connection,
            )
        if not merge_count:
            existing += 1

    invitro = _active_brand_ids(connection, name="Инвитро", channel="offline", mcc="8071")
    if invitro:
        offer = connection.execute(
            "SELECT brand_id FROM partner_offers WHERE source_key='cashalot:invitro'"
        ).fetchone()
        target_id = offer["brand_id"] if offer and offer["brand_id"] in invitro else min(invitro)
        for source_id in invitro:
            if source_id == target_id:
                continue
            stores.apply_change(
                "merge_brand",
                {"brand_id": source_id, "target_id": target_id},
                actor_id,
                connection=connection,
            )
            applied += 1
        if len(invitro) == 1:
            existing += 1
    network_applied, network_existing = _merge_reviewed_cross_channel_networks(
        stores, connection, actor_id=actor_id
    )
    applied += network_applied
    existing += network_existing
    return applied, existing


def _preflight_offer_updates(
    partners: PartnerRepository, connection, raw: dict
) -> tuple[list[tuple[int, PartnerOfferInput]], list[int], dict[str, int]]:
    updates: list[tuple[int, PartnerOfferInput]] = []
    removals: list[int] = []
    counters = {
        "offers_updated": 0,
        "offers_already_current": 0,
        "offers_removed": 0,
        "offers_already_removed": 0,
        "human_edits_preserved": 0,
    }
    desired = {
        item["source_key"]: item
        for item in raw["offers"]
        if item["card_id"] in {_CASHALOT_CARD, _STATUS_CARD}
    }
    conflicts: list[str] = []
    for source_key, item in desired.items():
        row = connection.execute(
            "SELECT id FROM partner_offers WHERE source_key=?", (source_key,)
        ).fetchone()
        if row is None:
            continue
        offer = partners.get_offer(row["id"], include_archived=True, connection=connection)
        if offer is None:
            continue
        if offer.archived:
            counters["human_edits_preserved"] += 1
            continue
        wanted = _input(item, offer.brand_id)
        if _offer_signature(offer) == _input_signature(wanted):
            counters["offers_already_current"] += 1
            continue
        legacy = wanted
        if offer.card_id == _CASHALOT_CARD and wanted.channel == "offline":
            legacy = replace(wanted, channel="any")
        elif offer.card_id == _STATUS_CARD:
            legacy = replace(
                wanted,
                tiers=(PartnerTierInput(Decimal("3.2")),),
                conditions=wanted.conditions.rstrip("."),
                source_url="https://stbank.by/",
            )
        if _offer_signature(offer) != _input_signature(legacy):
            conflicts.append(source_key)
            continue
        updates.append((offer.id, wanted))

    for source_key, (value, source_url) in REMOVED_CASHALOT.items():
        row = connection.execute(
            "SELECT id FROM partner_offers WHERE source_key=?", (source_key,)
        ).fetchone()
        if row is None:
            counters["offers_already_removed"] += 1
            continue
        offer = partners.get_offer(row["id"], include_archived=True, connection=connection)
        if offer is None or offer.archived:
            counters["offers_already_removed"] += 1
            continue
        if _offer_signature(offer) != _input_signature(_legacy_removed(offer, value, source_url)):
            conflicts.append(source_key)
            continue
        removals.append(offer.id)

    if conflicts:
        raise PartnerCleanupError(
            "Не перезаписаны изменённые помощниками предложения: "  # noqa: RUF001
            + ", ".join(conflicts)
        )
    return updates, removals, counters


def _hide_empty_xistore(
    stores: StoreRepository, connection, *, actor_id: int, brand_id: int | None
) -> int:
    if brand_id is None:
        return 0
    if connection.execute(
        """SELECT 1 FROM store_brand_members bm
        JOIN store_facts f ON f.merchant_id=bm.merchant_id
        WHERE bm.brand_id=? AND f.archived=0 LIMIT 1""",
        (brand_id,),
    ).fetchone():
        return 0
    if (
        connection.execute(
            "SELECT 1 FROM partner_offers WHERE brand_id=? AND archived=0 LIMIT 1", (brand_id,)
        ).fetchone()
        or connection.execute(
            "SELECT 1 FROM partner_exclusions WHERE brand_id=? AND archived=0 LIMIT 1", (brand_id,)
        ).fetchone()
    ):
        return 0
    merchant_ids = [
        row["id"]
        for row in connection.execute(
            """SELECT m.id FROM store_brand_members bm
            JOIN store_merchants m ON m.id=bm.merchant_id
            WHERE bm.brand_id=? AND m.archived=0 ORDER BY m.id""",
            (brand_id,),
        )
    ]
    for merchant_id in merchant_ids:
        stores.apply_change(
            "archive_merchant", {"merchant_id": merchant_id}, actor_id, connection=connection
        )
    return len(merchant_ids)


def apply_partner_cleanup(
    stores: StoreRepository,
    partners: PartnerRepository,
    *,
    actor_id: int,
    path: Path = SEED_PATH,
) -> dict[str, int]:
    """Reconcile only untouched legacy rows in one auditable transaction."""

    if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id < 1:
        raise PartnerCleanupError("Нужен положительный ID владельца для аудита")
    raw = load_seed(path)
    partners.initialize()
    with stores.transaction() as connection:
        merged, merges_existing = _merge_reviewed_duplicates(stores, connection, actor_id=actor_id)
        # Preflight after the merges so update payloads use the surviving brand.
        # Any conflict still rolls the entire transaction, including the merges,
        # back to its original state.
        updates, removals, counters = _preflight_offer_updates(partners, connection, raw)
        xistore_brand = connection.execute(
            "SELECT brand_id FROM partner_offers WHERE source_key='cashalot:xistore-by'"
        ).fetchone()
        for offer_id, payload in updates:
            partners.update_offer(offer_id, payload, actor_id=actor_id, connection=connection)
            counters["offers_updated"] += 1
        for offer_id in removals:
            partners.delete_offer(offer_id, actor_id=actor_id, connection=connection)
            counters["offers_removed"] += 1
        hidden = _hide_empty_xistore(
            stores,
            connection,
            actor_id=actor_id,
            brand_id=xistore_brand["brand_id"] if xistore_brand else None,
        )
        return {
            "merges_applied": merged,
            "merges_existing": merges_existing,
            **counters,
            "empty_merchants_hidden": hidden,
        }


def main(argv: list[str] | None = None) -> None:
    """Apply the reviewed partner correction to the configured stores database."""

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("MCC_STORES_PATH", "var/stores.sqlite3")),
    )
    parser.add_argument("--seed", type=Path, default=SEED_PATH)
    args = parser.parse_args(argv)
    try:
        actor_id = int(os.environ.get("BOT_OWNER_TELEGRAM_ID", ""))
    except ValueError as exc:
        raise SystemExit("BOT_OWNER_TELEGRAM_ID must be a positive integer") from exc
    stores = StoreRepository(args.database)
    stores.initialize()
    partners = PartnerRepository(stores)
    try:
        result = apply_partner_cleanup(stores, partners, actor_id=actor_id, path=args.seed)
    except (PartnerCleanupError, PartnerRewardError, StoreError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
