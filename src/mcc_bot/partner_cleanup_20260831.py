"""Reconcile the reviewed 2026-08-31 partner correction atomically.

Only the named 21век and Unistore variants are canonicalized.  Stable seeded
rows are corrected only while they still exactly match the preceding static
snapshot; a moderator edit aborts the whole transaction instead of being
overwritten.
"""

# ruff: noqa: RUF001, RUF002

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .partner_cleanup_20260830 import _input, _input_signature, _offer_signature
from .partner_rewards import (
    PartnerExclusion,
    PartnerExclusionInput,
    PartnerOfferInput,
    PartnerRepository,
    PartnerRewardError,
)
from .partner_seed_20260830 import SEED_PATH as PREVIOUS_SEED_PATH
from .partner_seed_20260830 import _date, load_seed
from .partner_seed_20260831 import SEED_PATH
from .stores import StoreError, StoreRepository, normalize_store_name

_STATUS_CARD = "statusbank_statuskarta"

_IDENTITIES = (
    {
        "name": "21век",
        "aliases": ("21vek", "21vek.by", "21 век"),
        "offer_keys": (
            "cashalot:21vek-by",
            "bnb-1-2-3:a72c0ccd",
            "plushki:promo:01",
            "statuskarta:21vek:2026-08-30",
        ),
        "exclusion_keys": (),
    },
    {
        "name": "Unistore",
        "aliases": ("Unistore опт&розница", "Юнистор", "Юнисторе", "Uni store"),
        "offer_keys": ("cashalot:unistore-opt-roznitsa",),
        "exclusion_keys": ("plushki:exclusion:1",),
    },
)


class PartnerCleanupError(RuntimeError):
    """Raised when safe reconciliation cannot be proven."""


def _exclusion_input(item: dict, brand_id: int | None) -> PartnerExclusionInput:
    return PartnerExclusionInput(
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


def _exclusion_signature(exclusion: PartnerExclusion) -> tuple:
    return (
        exclusion.card_id,
        exclusion.reward_kind,
        exclusion.channel,
        exclusion.mcc,
        exclusion.starts_on,
        exclusion.ends_on,
        exclusion.suppress_base,
        exclusion.reason,
        exclusion.source_url,
    )


def _exclusion_input_signature(payload: PartnerExclusionInput) -> tuple:
    return (
        payload.card_id.strip(),
        payload.reward_kind,
        payload.channel,
        payload.mcc,
        payload.starts_on,
        payload.ends_on,
        payload.suppress_base,
        payload.reason.strip(),
        payload.source_url.strip(),
    )


def _items_by_key(raw: dict, section: str) -> dict[str, dict]:
    return {item["source_key"]: item for item in raw[section]}


def _candidate_brand_ids(connection, *, name: str, aliases: tuple[str, ...]) -> list[int]:
    allowed = {normalize_store_name(value) for value in (name, *aliases)}
    result = []
    for row in connection.execute(
        "SELECT id,name,aliases_json FROM store_brands WHERE archived=0 ORDER BY id"
    ).fetchall():
        names = (row["name"], *json.loads(row["aliases_json"]))
        if any(normalize_store_name(value) in allowed for value in names):
            result.append(row["id"])
    return result


def _assert_seed_rows_belong_to_candidates(
    connection,
    *,
    offer_keys: tuple[str, ...],
    exclusion_keys: tuple[str, ...],
    candidate_ids: list[int],
) -> None:
    candidates = set(candidate_ids)
    conflicts = []
    for table, keys in (
        ("partner_offers", offer_keys),
        ("partner_exclusions", exclusion_keys),
    ):
        for source_key in keys:
            row = connection.execute(
                f"SELECT brand_id FROM {table} WHERE source_key=?", (source_key,)
            ).fetchone()
            if row is not None and row["brand_id"] not in candidates:
                conflicts.append(source_key)
    if conflicts:
        raise PartnerCleanupError(
            "Изменённая привязка статических партнёров не перезаписана: "
            + ", ".join(conflicts)
        )


def _preflight_updates(
    partners: PartnerRepository,
    connection,
    *,
    desired: dict,
    previous: dict,
) -> tuple[list[tuple[int, PartnerOfferInput]], list[tuple[int, PartnerExclusionInput]], int]:
    offer_updates = []
    exclusion_updates = []
    already_current = 0
    conflicts = []
    previous_offers = _items_by_key(previous, "offers")
    previous_exclusions = _items_by_key(previous, "exclusions")

    for item in desired["offers"]:
        source_key = item["source_key"]
        row = connection.execute(
            "SELECT id FROM partner_offers WHERE source_key=?", (source_key,)
        ).fetchone()
        if row is None:
            continue
        offer = partners.get_offer(row["id"], include_archived=True, connection=connection)
        assert offer is not None
        wanted = _input(item, offer.brand_id)
        if not offer.archived and _offer_signature(offer) == _input_signature(wanted):
            already_current += 1
            continue
        legacy_item = previous_offers.get(source_key)
        legacy = _input(legacy_item, offer.brand_id) if legacy_item is not None else None
        if (
            not offer.archived
            and legacy is not None
            and _offer_signature(offer) == _input_signature(legacy)
        ):
            offer_updates.append((offer.id, wanted))
            continue
        conflicts.append(source_key)

    for item in desired["exclusions"]:
        source_key = item["source_key"]
        row = connection.execute(
            "SELECT * FROM partner_exclusions WHERE source_key=?", (source_key,)
        ).fetchone()
        if row is None:
            continue
        exclusion = partners._exclusion_from_row(row)
        wanted = _exclusion_input(item, exclusion.brand_id)
        if not exclusion.archived and _exclusion_signature(
            exclusion
        ) == _exclusion_input_signature(wanted):
            already_current += 1
            continue
        legacy_item = previous_exclusions.get(source_key)
        legacy = (
            _exclusion_input(legacy_item, exclusion.brand_id)
            if legacy_item is not None
            else None
        )
        if (
            not exclusion.archived
            and legacy is not None
            and _exclusion_signature(exclusion) == _exclusion_input_signature(legacy)
        ):
            exclusion_updates.append((exclusion.id, wanted))
            continue
        conflicts.append(source_key)

    if conflicts:
        raise PartnerCleanupError(
            "Изменённые статические партнёрские правила не перезаписаны: "
            + ", ".join(conflicts)
        )
    return offer_updates, exclusion_updates, already_current


def _canonicalize_identity(
    stores: StoreRepository,
    connection,
    *,
    name: str,
    aliases: tuple[str, ...],
    candidate_ids: list[int],
    actor_id: int,
) -> tuple[int | None, int, int]:
    if not candidate_ids:
        return None, 0, 0
    target_id = min(
        candidate_ids,
        key=lambda brand_id: (
            stores.get_brand(brand_id, connection=connection).name != name,
            brand_id,
        ),
    )
    merged = 0
    for source_id in candidate_ids:
        if source_id == target_id:
            continue
        stores.apply_change(
            "merge_brand",
            {"brand_id": source_id, "target_id": target_id},
            actor_id,
            connection=connection,
        )
        merged += 1
    brand = stores.get_brand(target_id, connection=connection)
    if brand is None:
        raise PartnerCleanupError(f"Канонический магазин {name} исчез")
    final_aliases = tuple(
        dict.fromkeys(alias for alias in (*brand.aliases, *aliases) if alias != name)
    )
    renamed = int(brand.name != name or brand.aliases != final_aliases)
    if renamed:
        stores.apply_change(
            "edit_brand_names",
            {"brand_id": target_id, "name": name, "aliases": final_aliases},
            actor_id,
            connection=connection,
        )
    return target_id, merged, renamed


def apply_partner_cleanup(
    stores: StoreRepository,
    partners: PartnerRepository,
    *,
    actor_id: int,
    path: Path = SEED_PATH,
    previous_path: Path = PREVIOUS_SEED_PATH,
) -> dict[str, int]:
    """Apply the narrow correction once and return privacy-safe counters."""

    if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id < 1:
        raise PartnerCleanupError("Нужен положительный ID владельца для аудита")
    desired = load_seed(path)
    previous = load_seed(previous_path)
    partners.initialize()
    with stores.transaction() as connection:
        candidates = {
            identity["name"]: _candidate_brand_ids(
                connection, name=identity["name"], aliases=identity["aliases"]
            )
            for identity in _IDENTITIES
        }
        for identity in _IDENTITIES:
            _assert_seed_rows_belong_to_candidates(
                connection,
                offer_keys=identity["offer_keys"],
                exclusion_keys=identity["exclusion_keys"],
                candidate_ids=candidates[identity["name"]],
            )
        offer_updates, exclusion_updates, already_current = _preflight_updates(
            partners, connection, desired=desired, previous=previous
        )
        for offer_id, payload in offer_updates:
            partners.update_offer(offer_id, payload, actor_id=actor_id, connection=connection)
        for exclusion_id, payload in exclusion_updates:
            partners.update_exclusion(
                exclusion_id, payload, actor_id=actor_id, connection=connection
            )

        canonical: dict[str, int | None] = {}
        merged = renamed = 0
        for identity in _IDENTITIES:
            target_id, identity_merged, identity_renamed = _canonicalize_identity(
                stores,
                connection,
                name=identity["name"],
                aliases=identity["aliases"],
                candidate_ids=candidates[identity["name"]],
                actor_id=actor_id,
            )
            canonical[identity["name"]] = target_id
            merged += identity_merged
            renamed += identity_renamed

        status_archived = 0
        canonical_21 = canonical["21век"]
        rows = connection.execute(
            "SELECT id,brand_id FROM partner_offers WHERE card_id=? AND archived=0 ORDER BY id",
            (_STATUS_CARD,),
        ).fetchall()
        for row in rows:
            if canonical_21 is not None and row["brand_id"] == canonical_21:
                continue
            status_archived += int(
                partners.delete_offer(row["id"], actor_id=actor_id, connection=connection)
            )

        return {
            "brands_merged": merged,
            "brands_renamed": renamed,
            "offers_updated": len(offer_updates),
            "exclusions_updated": len(exclusion_updates),
            "rules_already_current": already_current,
            "status_offers_archived": status_archived,
        }


def main(argv: list[str] | None = None) -> None:
    """Apply the reviewed correction to the configured stores database."""

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("MCC_STORES_PATH", "var/stores.sqlite3")),
    )
    parser.add_argument("--seed", type=Path, default=SEED_PATH)
    parser.add_argument("--previous-seed", type=Path, default=PREVIOUS_SEED_PATH)
    args = parser.parse_args(argv)
    try:
        actor_id = int(os.environ.get("BOT_OWNER_TELEGRAM_ID", ""))
    except ValueError as exc:
        raise SystemExit("BOT_OWNER_TELEGRAM_ID must be a positive integer") from exc
    stores = StoreRepository(args.database)
    stores.initialize()
    partners = PartnerRepository(stores)
    try:
        result = apply_partner_cleanup(
            stores,
            partners,
            actor_id=actor_id,
            path=args.seed,
            previous_path=args.previous_seed,
        )
    except (PartnerCleanupError, PartnerRewardError, StoreError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
