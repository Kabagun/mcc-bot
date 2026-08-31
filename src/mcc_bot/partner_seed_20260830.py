"""Apply the reviewed 2026-08-30 official partner snapshot exactly once.

This is an explicit release-time command, not a crawler and not a startup job.
Stable source keys make reruns insert-missing/no-op: later moderator edits,
archives, and store renames are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from .partner_rewards import (
    PartnerExclusionInput,
    PartnerOfferInput,
    PartnerRepository,
    PartnerRewardError,
    PartnerTierInput,
)
from .resources import default_data_path
from .stores import StoreRepository

SEED_PATH = default_data_path("partner_seed_20260830.json")

_BrandResolution = Literal["created", "reused", "missing", "ambiguous"]


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _tier(raw: dict) -> PartnerTierInput:
    return PartnerTierInput(
        value=Decimal(raw["value"]),
        min_purchase=Decimal(raw["min_purchase"]) if raw.get("min_purchase") else None,
        max_purchase=Decimal(raw["max_purchase"]) if raw.get("max_purchase") else None,
        per_transaction_cap=(
            Decimal(raw["per_transaction_cap"]) if raw.get("per_transaction_cap") else None
        ),
        starts_on=_date(raw.get("starts_on")),
        ends_on=_date(raw.get("ends_on")),
    )


def load_seed(path: Path = SEED_PATH) -> dict:
    """Load the immutable UTF-8 snapshot and reject unsupported versions."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise PartnerRewardError("Версия статического набора партнёров должна быть равна 1")
    if not isinstance(raw.get("offers"), list) or not isinstance(raw.get("exclusions"), list):
        raise PartnerRewardError("Статический набор партнёров повреждён")
    return raw


def _brand_for_seed(
    stores: StoreRepository,
    connection,
    *,
    brand_key: str,
    name: str,
    aliases: tuple[str, ...],
    preferred_channel: str,
    actor_id: int,
    require_existing_mcc: bool = False,
    match_channel: str | None = None,
    match_mcc: str | None = None,
) -> tuple[int | None, _BrandResolution, int | None]:
    def has_required_mcc(brand_id: int) -> bool:
        brand = stores.get_brand(brand_id, connection=connection)
        if brand is None:
            return False
        facts = stores.list_brand_mcc(brand_id, connection=connection)
        return any(
            (match_channel is None or fact.channel == match_channel)
            and (match_mcc is None or fact.mcc == match_mcc)
            for fact in facts
        )

    mapped = connection.execute(
        "SELECT brand_id FROM partner_seed_brands WHERE source_key=?", (brand_key,)
    ).fetchone()
    mapped_brand_id = mapped["brand_id"] if mapped else None
    if mapped_brand_id is not None and (
        not require_existing_mcc or has_required_mcc(mapped_brand_id)
    ):
        return mapped_brand_id, "reused", None

    ranked_brands: dict[int, tuple[int, int, int]] = {}
    for name_position, candidate in enumerate((name, *aliases)):
        for channel in ("offline", "online"):
            for merchant in stores.find_exact(candidate, channel, connection=connection):
                brand = stores.brand_for_merchant(merchant.id, connection=connection)
                if brand is None:
                    continue
                channel_penalty = int(
                    preferred_channel in {"offline", "online"}
                    and merchant.channel != preferred_channel
                )
                score = (channel_penalty, name_position, brand.id)
                previous = ranked_brands.get(brand.id)
                if previous is None or score < previous:
                    ranked_brands[brand.id] = score

    if require_existing_mcc:
        eligible = [brand_id for brand_id in ranked_brands if has_required_mcc(brand_id)]
        if not eligible:
            return None, "missing", None
        if len(eligible) > 1:
            return None, "ambiguous", None
        brand_id = eligible[0]
    elif ranked_brands:
        brand_id = min(ranked_brands, key=ranked_brands.__getitem__)
    else:
        channel = preferred_channel if preferred_channel in {"offline", "online"} else "offline"
        result = stores.apply_change(
            "add_merchant",
            {"name": name, "aliases": aliases, "channel": channel},
            actor_id,
            connection=connection,
        )
        brand_id = result.brand_id
        connection.execute(
            "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?)",
            (brand_key, brand_id),
        )
        return brand_id, "created", None

    if mapped_brand_id is None:
        connection.execute(
            "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?)",
            (brand_key, brand_id),
        )
        repair_from = None
    else:
        # Defer a stale mapping update until the caller has verified that an
        # existing seeded entity is still untouched and can move with it.
        repair_from = mapped_brand_id if mapped_brand_id != brand_id else None
    return brand_id, "reused", repair_from


def _offer_matches_seed(offer, payload: PartnerOfferInput, *, brand_id: int) -> bool:
    return (
        not offer.archived
        and offer.brand_id == brand_id
        and offer.card_id == payload.card_id.strip()
        and offer.channel == payload.channel
        and offer.mode == payload.mode
        and offer.reward_kind == payload.reward_kind
        and offer.starts_on == payload.starts_on
        and offer.ends_on == payload.ends_on
        and offer.conditions == payload.conditions.strip()
        and offer.source_url == payload.source_url.strip()
        and tuple(
            (
                tier.value,
                tier.min_purchase,
                tier.max_purchase,
                tier.per_transaction_cap,
                tier.starts_on,
                tier.ends_on,
            )
            for tier in offer.tiers
        )
        == tuple(
            (
                tier.value,
                tier.min_purchase,
                tier.max_purchase,
                tier.per_transaction_cap,
                tier.starts_on,
                tier.ends_on,
            )
            for tier in payload.tiers
        )
    )


def _exclusion_matches_seed(exclusion, payload: PartnerExclusionInput, *, brand_id: int) -> bool:
    return (
        not exclusion.archived
        and exclusion.brand_id == brand_id
        and exclusion.card_id == payload.card_id.strip()
        and exclusion.reward_kind == payload.reward_kind
        and exclusion.channel == payload.channel
        and exclusion.mcc == payload.mcc
        and exclusion.starts_on == payload.starts_on
        and exclusion.ends_on == payload.ends_on
        and exclusion.suppress_base == payload.suppress_base
        and exclusion.reason == payload.reason.strip()
        and exclusion.source_url == payload.source_url.strip()
    )


def _commit_mapping_repair(connection, brand_key: str, brand_id: int) -> None:
    connection.execute(
        "UPDATE partner_seed_brands SET brand_id=? WHERE source_key=?",
        (brand_id, brand_key),
    )


def apply_partner_seed(
    stores: StoreRepository,
    partners: PartnerRepository,
    *,
    actor_id: int,
    path: Path = SEED_PATH,
) -> dict[str, int]:
    """Apply missing official rows atomically and return privacy-safe counts."""

    raw = load_seed(path)
    partners.initialize()
    counters = {
        "brands_created": 0,
        "brands_reused": 0,
        "brands_skipped_missing": 0,
        "brands_skipped_ambiguous": 0,
        "brand_mappings_repaired": 0,
        "brand_mapping_conflicts": 0,
        "offers_added": 0,
        "offers_existing": 0,
        "offers_skipped": 0,
        "exclusions_added": 0,
        "exclusions_existing": 0,
        "exclusions_skipped": 0,
    }
    with stores.transaction() as connection:
        for item in raw["offers"]:
            brand_id, resolution, repair_from = _brand_for_seed(
                stores,
                connection,
                brand_key=item["brand_key"],
                name=item["brand"],
                aliases=tuple(item.get("aliases", ())),
                preferred_channel=item["channel"],
                actor_id=actor_id,
                require_existing_mcc=bool(item.get("require_existing_mcc", False)),
                match_channel=item.get("match_channel"),
                match_mcc=item.get("match_mcc"),
            )
            if brand_id is None:
                counters[f"brands_skipped_{resolution}"] += 1
                counters["offers_skipped"] += 1
                continue
            counters[f"brands_{resolution}"] += 1
            payload = PartnerOfferInput(
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
            if repair_from is not None:
                row = connection.execute(
                    "SELECT id FROM partner_offers WHERE source_key=?", (item["source_key"],)
                ).fetchone()
                existing = (
                    partners.get_offer(row["id"], include_archived=True, connection=connection)
                    if row
                    else None
                )
                if existing is not None and existing.brand_id == brand_id:
                    pass
                elif existing is not None and _offer_matches_seed(
                    existing, payload, brand_id=repair_from
                ):
                    partners.update_offer(
                        existing.id, payload, actor_id=actor_id, connection=connection
                    )
                elif existing is not None:
                    counters["brand_mapping_conflicts"] += 1
                    counters["offers_skipped"] += 1
                    continue
                _commit_mapping_repair(connection, item["brand_key"], brand_id)
                counters["brand_mappings_repaired"] += 1
            _offer, added = partners.ensure_seed_offer(
                item["source_key"], payload, actor_id=actor_id, connection=connection
            )
            counters["offers_added" if added else "offers_existing"] += 1
        for item in raw["exclusions"]:
            if item.get("brand_key") is None:
                brand_id = None
            else:
                brand_id, resolution, repair_from = _brand_for_seed(
                    stores,
                    connection,
                    brand_key=item["brand_key"],
                    name=item["brand"],
                    aliases=tuple(item.get("aliases", ())),
                    preferred_channel=item["channel"],
                    actor_id=actor_id,
                    require_existing_mcc=bool(item.get("require_existing_mcc", False)),
                    match_channel=item.get("match_channel"),
                    match_mcc=item.get("match_mcc"),
                )
                if brand_id is None:
                    counters[f"brands_skipped_{resolution}"] += 1
                    counters["exclusions_skipped"] += 1
                    continue
                counters[f"brands_{resolution}"] += 1
            if item.get("brand_key") is None:
                repair_from = None
            payload = PartnerExclusionInput(
                brand_id=brand_id,
                card_id=item["card_id"],
                reward_kind=item["reward_kind"],
                channel=item["channel"],
                mcc=item.get("mcc"),
                starts_on=_date(item.get("starts_on")),
                ends_on=_date(item.get("ends_on")),
                suppress_base=bool(item.get("suppress_base", False)),
                reason=item.get("reason", ""),
                source_url=item.get("source_url", ""),
            )
            if repair_from is not None:
                row = connection.execute(
                    "SELECT * FROM partner_exclusions WHERE source_key=?", (item["source_key"],)
                ).fetchone()
                existing = partners._exclusion_from_row(row) if row else None
                if existing is not None and existing.brand_id == brand_id:
                    pass
                elif existing is not None and _exclusion_matches_seed(
                    existing, payload, brand_id=repair_from
                ):
                    partners.update_exclusion(
                        existing.id, payload, actor_id=actor_id, connection=connection
                    )
                elif existing is not None:
                    counters["brand_mapping_conflicts"] += 1
                    counters["exclusions_skipped"] += 1
                    continue
                _commit_mapping_repair(connection, item["brand_key"], brand_id)
                counters["brand_mappings_repaired"] += 1
            _exclusion, added = partners.ensure_seed_exclusion(
                item["source_key"], payload, actor_id=actor_id, connection=connection
            )
            counters["exclusions_added" if added else "exclusions_existing"] += 1
    return counters


def main(argv: list[str] | None = None) -> None:
    """Apply the static partner package to the configured stores database."""

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
    print(
        json.dumps(
            apply_partner_seed(stores, partners, actor_id=actor_id, path=args.seed),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
