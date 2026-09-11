"""Local, approval-gated preview/apply for versioned partner snapshots.

The command operates only on the explicitly selected SQLite database. Preview
opens it in query-only mode and computes a deterministic plan; apply recomputes
that plan, requires its SHA-256 token, takes a backup, and adds only rows which
are still safe to insert. Existing partner rows are never silently overwritten;
explicitly guarded retirements, source tombstones, and mapping repairs may be
applied atomically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .partner_rewards import (
    PartnerExclusionInput,
    PartnerOfferInput,
    PartnerRepository,
    PartnerRewardError,
    PartnerTierInput,
)
from .stores import StoreRepository

SNAPSHOT_VERSION = 1


class PartnerBatchError(ValueError):
    """Raised when a snapshot or approval plan cannot be safely applied."""


class StalePartnerPlanError(PartnerBatchError):
    """Raised when the target database or snapshot differs from the preview."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str | bytes) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def _write_json_result(result: dict[str, Any], *, stream: Any = None) -> None:
    """Write one UTF-8 JSON line even when Windows selected a legacy code page."""

    output = sys.stdout if stream is None else stream
    reconfigure = getattr(output, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    output.write(f"{_canonical(result)}\n")


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PartnerBatchError(f"{field} must be a decimal value")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PartnerBatchError(f"{field} must be a decimal value") from exc
    if not result.is_finite() or result < 0:
        raise PartnerBatchError(f"{field} must be a non-negative decimal value")
    return result


def _decimal_text(value: Any, field: str) -> str:
    result = _decimal(value, field)
    rendered = format(result, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _optional_decimal_text(value: Any, field: str) -> str | None:
    return None if value is None or value == "" else _decimal_text(value, field)


def _date_value(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise PartnerBatchError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise PartnerBatchError(f"{field} must be an ISO date") from exc


def _offer_mccs(raw: dict[str, Any]) -> tuple[str, ...] | None:
    """Read optional offer MCC scope without guessing from free text."""

    value = raw.get("mccs", raw.get("mcc"))
    if value is None or value == "":
        return None
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple, set)) or not values:
        raise PartnerBatchError("mccs must be a four-digit MCC or a list of MCCs")
    result = []
    for item in values:
        if not isinstance(item, str) or len(item) != 4 or not item.isdigit():
            raise PartnerBatchError("mccs must contain only four-digit MCCs")
        result.append(item)
    return tuple(sorted(set(result)))


def _text(value: Any, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise PartnerBatchError(f"{field} must be text")
    result = value.strip()
    if required and not result:
        raise PartnerBatchError(f"{field} is required")
    return result


def _aliases(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise PartnerBatchError("aliases must be a list")
    return tuple(_text(item, "aliases[]", required=True) for item in value)


def _normalize_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    """Copy snapshot rows while canonicalizing their stable lookup keys."""

    normalized = dict(raw)
    for field in (
        "offers",
        "exclusions",
        "offer_retirements",
        "manual_offer_retirements",
        "partner_mapping_repairs",
        "partner_seed_tombstones",
    ):
        rows = []
        for item in raw.get(field, []):
            if not isinstance(item, dict):
                rows.append(item)
                continue
            row = dict(item)
            for key in ("source_key", "brand_key", "offer_source_key"):
                value = row.get(key)
                if isinstance(value, str):
                    row[key] = value.strip()
            rows.append(row)
        if field in {"offers", "exclusions"} or field in raw:
            normalized[field] = rows
    return normalized


def _snapshot_source_key_index(
    raw: dict[str, Any],
) -> dict[str, list[tuple[str, int]]]:
    """Index every structurally usable snapshot source key before validation."""

    index: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for field, kind in (
        ("offers", "offer"),
        ("exclusions", "exclusion"),
        ("offer_retirements", "offer_retirement"),
        ("partner_seed_tombstones", "partner_seed_tombstone"),
    ):
        for position, item in enumerate(raw.get(field, [])):
            if not isinstance(item, dict):
                continue
            source_key = item.get("source_key")
            if isinstance(source_key, str) and source_key:
                index[source_key].append((kind, position))
    for position, item in enumerate(raw.get("partner_mapping_repairs", [])):
        if not isinstance(item, dict):
            continue
        for key, kind in (
            ("source_key", "partner_mapping_repair"),
            ("offer_source_key", "offer_rebind"),
        ):
            source_key = item.get(key)
            if isinstance(source_key, str) and source_key:
                index[source_key].append((kind, position))
    return index


def _snapshot_operation_rows(raw: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Index normalized source rows used to rebuild apply-time payloads."""

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for field, kind in (
        ("offers", "offer"),
        ("exclusions", "exclusion"),
        ("offer_retirements", "offer_retirement"),
    ):
        for item in raw.get(field, []):
            if not isinstance(item, dict):
                continue
            source_key = item.get("source_key")
            if isinstance(source_key, str) and source_key:
                result.setdefault((kind, source_key), item)
    return result


def load_snapshot(source: Path | str | dict[str, Any]) -> dict[str, Any]:
    """Load and structurally validate a version-1 partner snapshot.

    Semantic row errors are kept for the preview as ``snapshot_problem``
    conflicts, so a reviewer can see every bad row in one deterministic report.
    """

    if isinstance(source, dict):
        raw = source
    else:
        path = Path(source)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PartnerBatchError(f"Unable to load snapshot: {path}") from exc
    if not isinstance(raw, dict) or raw.get("version") != SNAPSHOT_VERSION:
        raise PartnerBatchError(f"Snapshot version must be {SNAPSHOT_VERSION}")
    if not isinstance(raw.get("offers"), list) or not isinstance(raw.get("exclusions"), list):
        raise PartnerBatchError("Snapshot must contain offers and exclusions lists")
    if "offer_retirements" in raw and not isinstance(raw["offer_retirements"], list):
        raise PartnerBatchError("Snapshot offer_retirements must be a list")
    for field in (
        "manual_offer_retirements",
        "partner_mapping_repairs",
        "partner_seed_tombstones",
    ):
        if field in raw and not isinstance(raw[field], list):
            raise PartnerBatchError(f"Snapshot {field} must be a list")
    if "problems" in raw and not isinstance(raw["problems"], list):
        raise PartnerBatchError("Snapshot problems must be a list")
    return _normalize_snapshot(raw)


def _snapshot_sha(raw: dict[str, Any]) -> str:
    return _sha256(_canonical(raw))


@contextmanager
def _read_connection(stores: StoreRepository) -> Iterator[sqlite3.Connection]:
    """Open an existing target without allowing preview writes."""

    if not getattr(stores, "_memory", False) and not stores.path.exists():
        raise PartnerBatchError(f"Database does not exist: {stores.path}")
    with stores.connection() as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection


def _require_schema(connection: sqlite3.Connection) -> None:
    required = {
        "store_brands",
        "store_merchants",
        "store_brand_members",
        "partner_offers",
        "partner_offer_tiers",
        "partner_exclusions",
        "partner_seed_brands",
    }
    present = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = sorted(required - present)
    if missing:
        raise PartnerBatchError("Database is missing required schema: " + ", ".join(missing))


def _database_fingerprint(connection: sqlite3.Connection) -> str:
    """Hash the complete visible SQLite dump, including schema and all rows."""

    return _sha256("\n".join(connection.iterdump()))


def _active_brand(connection: sqlite3.Connection, brand_id: int) -> bool:
    row = connection.execute(
        "SELECT archived,merged_into FROM store_brands WHERE id=?", (brand_id,)
    ).fetchone()
    return row is not None and not row["archived"] and row["merged_into"] is None


def _brand_candidates(
    stores: StoreRepository,
    connection: sqlite3.Connection,
    raw: dict[str, Any],
) -> tuple[int, ...]:
    names = (_text(raw.get("brand"), "brand", required=True), *_aliases(raw.get("aliases", ())))
    requested_channel = raw.get("channel")
    channels = (
        (requested_channel,)
        if requested_channel in {"offline", "online"}
        else ("offline", "online")
    )
    candidates: set[int] = set()
    for name in names:
        for channel in channels:
            for merchant in stores.find_exact(name, channel, connection=connection):
                brand = stores.brand_for_merchant(merchant.id, connection=connection)
                if brand is not None and not brand.archived:
                    candidates.add(brand.id)
    # A brand may have a reviewed name/alias without a current merchant branch.
    needle_names = {name.casefold() for name in names}
    for row in connection.execute(
        "SELECT id,name,aliases_json,merged_into FROM store_brands WHERE archived=0"
    ):
        try:
            brand_names = {row["name"], *json.loads(row["aliases_json"])}
        except (TypeError, json.JSONDecodeError):
            brand_names = {row["name"]}
        if any(str(name).strip().casefold() in needle_names for name in brand_names):
            if row["merged_into"]:
                continue
            candidates.add(row["id"])
    if raw.get("require_existing_mcc"):
        match_channel = raw.get("match_channel")
        match_mcc = raw.get("match_mcc")
        eligible: set[int] = set()
        for brand_id in candidates:
            rows = connection.execute(
                """SELECT f.mcc,m.channel FROM store_facts f
                   JOIN store_brand_members bm ON bm.merchant_id=f.merchant_id
                   JOIN store_merchants m ON m.id=f.merchant_id
                   WHERE bm.brand_id=? AND f.archived=0 AND m.archived=0""",
                (brand_id,),
            )
            if any(
                (match_channel is None or row["channel"] == match_channel)
                and (match_mcc is None or row["mcc"] == match_mcc)
                for row in rows
            ):
                eligible.add(brand_id)
        candidates = eligible
    return tuple(sorted(candidates))


def _brand_creation_descriptor(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the identity fields shared by a planned partner-only brand."""

    return {
        "name": _text(raw.get("brand"), "brand", required=True),
        "aliases": list(_aliases(raw.get("aliases", ()))),
    }


def _planned_brand_context(raw_items: list[Any]) -> dict[str, dict[str, Any]]:
    """Group candidate brand creations and block inconsistent identities."""

    context: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict) or raw.get("brand_key") is None:
            continue
        try:
            brand_key = _text(raw.get("brand_key"), "brand_key", required=True)
            descriptor = _brand_creation_descriptor(raw)
        except PartnerBatchError:
            continue
        descriptor_key = _canonical(descriptor)
        previous = context.get(brand_key)
        if previous is None:
            context[brand_key] = {
                **descriptor,
                "descriptor_key": descriptor_key,
                "channels": [raw.get("channel")],
                "preferred_channels": [raw.get("preferred_channel")],
                "blocked": bool(raw.get("require_existing_mcc")),
            }
        else:
            if previous["descriptor_key"] != descriptor_key:
                previous["blocked"] = True
            if raw.get("require_existing_mcc"):
                previous["blocked"] = True
            previous["channels"].append(raw.get("channel"))
            previous["preferred_channels"].append(raw.get("preferred_channel"))
    for planned in context.values():
        preferred = {value for value in planned["preferred_channels"] if value is not None}
        if any(value not in {"offline", "online"} for value in preferred):
            planned["blocked"] = True
        if len(preferred) > 1:
            planned["blocked"] = True
        if preferred:
            planned["channel"] = sorted(preferred)[0]
        else:
            planned["channel"] = (
                "offline"
                if any(value in {"offline", "any"} for value in planned["channels"])
                else "online"
            )
    return context


def _resolve_brand(
    stores: StoreRepository,
    connection: sqlite3.Connection,
    raw: dict[str, Any],
    planned_brands: dict[str, dict[str, Any]] | None = None,
) -> tuple[int | None, str, str | None]:
    direct = raw.get("brand_id")
    if direct is not None:
        if (
            isinstance(direct, bool)
            or not isinstance(direct, int)
            or not _active_brand(connection, direct)
        ):
            return None, "missing_brand", "brand_id is missing or inactive"
        return direct, "resolved", None
    brand_key = raw.get("brand_key")
    if brand_key is not None:
        brand_key = _text(brand_key, "brand_key", required=True)
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='partner_seed_brands'"
        ).fetchone()
        if table:
            mapping = connection.execute(
                "SELECT brand_id FROM partner_seed_brands WHERE source_key=?", (brand_key,)
            ).fetchone()
            if mapping is not None:
                mapped_brand_id = mapping["brand_id"]
                if not _active_brand(connection, mapped_brand_id):
                    return (
                        None,
                        "mapping_conflict",
                        "existing brand mapping points to inactive brand",
                    )
                candidates = _brand_candidates(stores, connection, raw)
                if candidates and mapped_brand_id not in candidates:
                    return (
                        None,
                        "mapping_conflict",
                        "existing brand mapping disagrees with exact brand candidates",
                    )
                if raw.get("require_existing_mcc") and mapped_brand_id not in candidates:
                    return (
                        None,
                        "mapping_conflict",
                        "existing brand mapping does not satisfy required MCC",
                    )
                return mapped_brand_id, "resolved", None
    candidates = _brand_candidates(stores, connection, raw)
    if len(candidates) == 1:
        return candidates[0], "resolved", None
    if not candidates:
        if not raw.get("require_existing_mcc"):
            brand_key = raw.get("brand_key")
            if isinstance(brand_key, str) and planned_brands is not None:
                planned = planned_brands.get(brand_key.strip())
                if planned is not None and not planned["blocked"]:
                    return None, "planned_creation", None
        return None, "missing_brand", "no active brand matched brand/aliases"
    return None, "ambiguous_brand", "multiple active brands matched brand/aliases"


def _offer_input(
    raw: dict[str, Any], brand_id: int | None
) -> tuple[PartnerOfferInput, dict[str, Any]]:
    tiers_raw = raw.get("tiers")
    if not isinstance(tiers_raw, list) or not tiers_raw:
        raise PartnerBatchError("tiers must be a non-empty list")
    tiers: list[PartnerTierInput] = []
    tier_records: list[dict[str, Any]] = []
    for index, item in enumerate(tiers_raw):
        if not isinstance(item, dict):
            raise PartnerBatchError(f"tiers[{index}] must be an object")
        value = _decimal(item.get("value"), f"tiers[{index}].value")
        minimum = _optional_decimal_text(item.get("min_purchase"), f"tiers[{index}].min_purchase")
        maximum = _optional_decimal_text(item.get("max_purchase"), f"tiers[{index}].max_purchase")
        cap = _optional_decimal_text(
            item.get("per_transaction_cap"), f"tiers[{index}].per_transaction_cap"
        )
        starts = _date_value(item.get("starts_on"), f"tiers[{index}].starts_on")
        ends = _date_value(item.get("ends_on"), f"tiers[{index}].ends_on")
        tier = PartnerTierInput(
            value=value,
            min_purchase=Decimal(minimum) if minimum is not None else None,
            max_purchase=Decimal(maximum) if maximum is not None else None,
            per_transaction_cap=Decimal(cap) if cap is not None else None,
            starts_on=date.fromisoformat(starts) if starts else None,
            ends_on=date.fromisoformat(ends) if ends else None,
        )
        tiers.append(tier)
        tier_records.append(
            {
                "value": _decimal_text(value, f"tiers[{index}].value"),
                "min_purchase": minimum,
                "max_purchase": maximum,
                "per_transaction_cap": cap,
                "starts_on": starts,
                "ends_on": ends,
            }
        )
    payload = PartnerOfferInput(
        brand_id=brand_id,
        card_id=_text(raw.get("card_id"), "card_id", required=True),
        channel=_text(raw.get("channel"), "channel", required=True),
        mode=_text(raw.get("mode"), "mode", required=True),
        reward_kind=_text(raw.get("reward_kind"), "reward_kind", required=True),
        tiers=tuple(tiers),
        starts_on=(
            date.fromisoformat(starts)
            if (starts := _date_value(raw.get("starts_on"), "starts_on"))
            else None
        ),
        ends_on=(
            date.fromisoformat(ends)
            if (ends := _date_value(raw.get("ends_on"), "ends_on"))
            else None
        ),
        conditions=_text(raw.get("conditions", ""), "conditions"),
        source_url=_text(raw.get("source_url", ""), "source_url"),
    )
    # Use the repository's validation contract before any apply-time write.
    from .partner_rewards import _validate_offer

    # Planned offers have no persisted brand yet.  Validate them against a
    # harmless positive placeholder while keeping ``payload``/``record``
    # brand_id=None for planning and planned-brand creation.
    _validate_offer(replace(payload, brand_id=brand_id if brand_id is not None else 1))
    record = {
        "brand_id": brand_id,
        "card_id": payload.card_id.strip(),
        "channel": payload.channel,
        "mode": payload.mode,
        "reward_kind": payload.reward_kind,
        "starts_on": payload.starts_on.isoformat() if payload.starts_on else None,
        "ends_on": payload.ends_on.isoformat() if payload.ends_on else None,
        "conditions": payload.conditions.strip(),
        "source_url": payload.source_url.strip(),
        "tiers": tier_records,
    }
    return payload, record


def _exclusion_input(
    raw: dict[str, Any], brand_id: int | None
) -> tuple[PartnerExclusionInput, dict[str, Any]]:
    mcc = raw.get("mcc")
    if mcc is not None:
        mcc = _text(mcc, "mcc", required=True)
    payload = PartnerExclusionInput(
        brand_id=brand_id,
        card_id=_text(raw.get("card_id"), "card_id", required=True),
        reward_kind=_text(raw.get("reward_kind"), "reward_kind", required=True),
        channel=_text(raw.get("channel"), "channel", required=True),
        mcc=mcc,
        starts_on=(
            date.fromisoformat(starts)
            if (starts := _date_value(raw.get("starts_on"), "starts_on"))
            else None
        ),
        ends_on=(
            date.fromisoformat(ends)
            if (ends := _date_value(raw.get("ends_on"), "ends_on"))
            else None
        ),
        suppress_base=raw.get("suppress_base", False),
        reason=_text(raw.get("reason", ""), "reason"),
        source_url=_text(raw.get("source_url", ""), "source_url"),
    )
    from .partner_rewards import _validate_exclusion

    # ``None`` is a valid global exclusion identity, but use a placeholder for
    # the same complete validation contract used by persisted exclusions.
    _validate_exclusion(replace(payload, brand_id=brand_id if brand_id is not None else 1))
    record = {
        "brand_id": brand_id,
        "card_id": payload.card_id.strip(),
        "reward_kind": payload.reward_kind,
        "channel": payload.channel,
        "mcc": payload.mcc,
        "starts_on": payload.starts_on.isoformat() if payload.starts_on else None,
        "ends_on": payload.ends_on.isoformat() if payload.ends_on else None,
        "suppress_base": bool(payload.suppress_base),
        "reason": payload.reason.strip(),
        "source_url": payload.source_url.strip(),
    }
    return payload, record


def _db_offer_record(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    tiers = []
    for tier in connection.execute(
        "SELECT * FROM partner_offer_tiers WHERE offer_id=? ORDER BY position,id", (row["id"],)
    ):
        tiers.append(
            {
                "value": _decimal_text(tier["value"], "stored tier value"),
                "min_purchase": _optional_decimal_text(tier["min_purchase"], "stored min_purchase"),
                "max_purchase": _optional_decimal_text(tier["max_purchase"], "stored max_purchase"),
                "per_transaction_cap": _optional_decimal_text(
                    tier["per_transaction_cap"], "stored cap"
                ),
                "starts_on": tier["starts_on"],
                "ends_on": tier["ends_on"],
            }
        )
    return {
        "brand_id": row["brand_id"],
        "card_id": row["card_id"],
        "channel": row["channel"],
        "mode": row["mode"],
        "reward_kind": row["reward_kind"],
        "starts_on": row["starts_on"],
        "ends_on": row["ends_on"],
        "conditions": row["conditions"],
        "source_url": row["source_url"],
        "tiers": tiers,
    }


def _db_exclusion_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "brand_id": row["brand_id"],
        "card_id": row["card_id"],
        "reward_kind": row["reward_kind"],
        "channel": row["channel"],
        "mcc": row["mcc"],
        "starts_on": row["starts_on"],
        "ends_on": row["ends_on"],
        "suppress_base": bool(row["suppress_base"]),
        "reason": row["reason"],
        "source_url": row["source_url"],
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """Return one table's columns without assuming the current schema revision."""

    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _active_linked_exclusion_ids(connection: sqlite3.Connection) -> set[int]:
    """Return active exclusions which are explicitly owned by an offer.

    The first partner-exclusion schema did not have ``offer_id``.  Such a
    database cannot contain an explicitly linked exclusion, so keep the old
    schema usable for ordinary (non-repair) batches.
    """

    if "offer_id" not in _table_columns(connection, "partner_exclusions"):
        return set()
    return {
        row["offer_id"]
        for row in connection.execute(
            "SELECT offer_id FROM partner_exclusions WHERE offer_id IS NOT NULL AND archived=0"
        )
    }


_OFFER_RECORD_FIELDS = frozenset(
    {
        "brand_id",
        "card_id",
        "channel",
        "mode",
        "reward_kind",
        "starts_on",
        "ends_on",
        "conditions",
        "source_url",
        "tiers",
    }
)
_TIER_RECORD_FIELDS = frozenset(
    {
        "value",
        "min_purchase",
        "max_purchase",
        "per_transaction_cap",
        "starts_on",
        "ends_on",
    }
)


def _offer_guard_record(value: Any, field: str) -> dict[str, Any]:
    """Validate and canonicalize a complete persisted offer semantic guard."""

    if not isinstance(value, dict):
        raise PartnerBatchError(f"{field} must be an object")
    missing = sorted(_OFFER_RECORD_FIELDS - value.keys())
    if missing:
        raise PartnerBatchError(f"{field} is missing: {', '.join(missing)}")
    if not isinstance(value.get("tiers"), list) or not value["tiers"]:
        raise PartnerBatchError(f"{field}.tiers must be a non-empty list")
    for index, tier in enumerate(value["tiers"]):
        if not isinstance(tier, dict):
            raise PartnerBatchError(f"{field}.tiers[{index}] must be an object")
        missing_tier = sorted(_TIER_RECORD_FIELDS - tier.keys())
        if missing_tier:
            raise PartnerBatchError(f"{field}.tiers[{index}] is missing: {', '.join(missing_tier)}")
    brand_id = value.get("brand_id")
    if isinstance(brand_id, bool) or not isinstance(brand_id, int) or brand_id < 1:
        raise PartnerBatchError(f"{field}.brand_id must be a positive integer")
    raw = dict(value)
    raw["brand_id"] = brand_id
    _payload, record = _offer_input(raw, brand_id)
    return record


def _conflict(kind: str, source_key: str | None, reason: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": kind, "source_key": source_key, "reason": reason}
    result.update(extra)
    return result


def _base_counts() -> dict[str, int]:
    return {
        "offers_new": 0,
        "offers_unchanged": 0,
        "offers_changed": 0,
        "offers_logical_duplicates": 0,
        "offers_archived": 0,
        "offers_tombstoned": 0,
        "offers_missing_brand": 0,
        "offers_ambiguous_brand": 0,
        "offers_mapping_conflict": 0,
        "offers_mapping_conflicts": 0,
        "offers_logical_overlaps": 0,
        "offers_snapshot_logical_overlaps": 0,
        "offers_snapshot_problems": 0,
        "exclusions_new": 0,
        "exclusions_unchanged": 0,
        "exclusions_changed": 0,
        "exclusions_logical_duplicates": 0,
        "exclusions_archived": 0,
        "exclusions_tombstoned": 0,
        "exclusions_missing_brand": 0,
        "exclusions_ambiguous_brand": 0,
        "exclusions_mapping_conflict": 0,
        "exclusions_mapping_conflicts": 0,
        "exclusions_snapshot_problems": 0,
        "snapshot_problems": 0,
        "brands_new": 0,
        "brand_mappings_new": 0,
        "partner_seed_tombstones_new": 0,
        "approved_repairs": 0,
        "approved_archives": 0,
        "approved_inserts": 0,
        "approved_operations": 0,
        "approved_partner_rows": 0,
        "source_key_conflicts": 0,
        "conflicts": 0,
    }


def _snapshot_source_keys(raw_items: list[Any]) -> set[str]:
    """Return normalized source keys from structurally usable snapshot rows."""

    result: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        source_key = raw.get("source_key")
        if isinstance(source_key, str) and source_key.strip():
            result.add(source_key.strip())
    return result


def _retirement_operation(
    source_key: str | None,
    status: str,
    action: str,
    *,
    existing: sqlite3.Row | None = None,
    record: dict[str, Any] | None = None,
    existing_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a stable, non-executable offer-retirement plan operation."""

    operation: dict[str, Any] = {
        "kind": "offer_retirement",
        "source_key": source_key,
        "status": status,
        "action": action,
        "offer_id": existing["id"] if existing is not None else None,
        "brand_id": existing["brand_id"] if existing is not None else None,
        "brand_key": None,
        "mccs": None,
        "record": record,
    }
    if existing_record is not None:
        operation["existing_record"] = existing_record
    return operation


def _analyse_retirements(
    raw_items: list[Any],
    *,
    existing_rows: dict[str, sqlite3.Row],
    existing_records: dict[int, dict[str, Any]],
    new_offer_keys: set[str],
    blocked_source_keys: set[str],
    active_linked_exclusion_ids: set[int],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, int],
    set[int],
    set[str],
]:
    """Classify explicitly requested legacy offer retirements fail-closed.

    Expected records are parsed with the persisted row's brand ID.  This keeps
    the comparison independent of current store names and aliases while still
    applying the normal offer/tier validation rules.
    """

    operations: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    safe_offer_ids: set[int] = set()
    collision_keys: set[str] = set()
    keyed_entries: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)

    def record_problem(source_key: str | None, reason: str) -> None:
        counts["snapshot_problems"] = counts.get("snapshot_problems", 0) + 1
        conflicts.append(_conflict("snapshot_problem", source_key, reason))

    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            record_problem(None, f"offer_retirements[{index}] must be an object")
            operations.append(_retirement_operation(None, "snapshot_problem", "none"))
            continue
        try:
            source_key = _text(
                raw.get("source_key"),
                f"offer_retirements[{index}].source_key",
                required=True,
            )
        except PartnerBatchError as exc:
            record_problem(None, str(exc))
            operations.append(_retirement_operation(None, "snapshot_problem", "none"))
            continue
        keyed_entries[source_key].append((index, raw))

    for source_key in sorted(keyed_entries):
        entries = keyed_entries[source_key]
        existing = existing_rows.get(source_key)
        if source_key in blocked_source_keys:
            counts["source_key_conflict"] = counts.get("source_key_conflict", 0) + len(entries)
            for _index, _raw in entries:
                operations.append(
                    _retirement_operation(
                        source_key,
                        "source_key_conflict",
                        "none",
                        existing=existing,
                    )
                )
            continue
        if len(entries) > 1:
            counts["duplicate"] = counts.get("duplicate", 0) + len(entries)
            conflicts.append(
                _conflict(
                    "retirement_conflict",
                    source_key,
                    "source key appears more than once in offer_retirements",
                    duplicate_count=len(entries),
                )
            )
            if source_key in new_offer_keys:
                collision_keys.add(source_key)
                conflicts.append(
                    _conflict(
                        "retirement_conflict",
                        source_key,
                        "source key appears in both offer_retirements and offers",
                    )
                )
            for _index, _raw in entries:
                operations.append(
                    _retirement_operation(source_key, "duplicate", "none", existing=existing)
                )
            continue

        _index, raw = entries[0]
        existing_brand_id = existing["brand_id"] if existing is not None else 1
        try:
            _offer_mccs(raw)
            _payload, expected_record = _offer_input(raw, existing_brand_id)
        except (PartnerBatchError, PartnerRewardError, KeyError, TypeError, ValueError) as exc:
            counts["snapshot_problems"] = counts.get("snapshot_problems", 0) + 1
            conflicts.append(_conflict("snapshot_problem", source_key, str(exc)))
            operations.append(
                _retirement_operation(source_key, "snapshot_problem", "none", existing=existing)
            )
            continue
        if existing is None:
            # A placeholder brand was used only to invoke the full validator;
            # never expose it as if it were a persisted identity.
            expected_record["brand_id"] = None

        if source_key in new_offer_keys:
            collision_keys.add(source_key)
            counts["conflict"] = counts.get("conflict", 0) + 1
            conflicts.append(
                _conflict(
                    "retirement_conflict",
                    source_key,
                    "source key appears in both offer_retirements and offers",
                )
            )
            operations.append(
                _retirement_operation(
                    source_key,
                    "conflict",
                    "none",
                    existing=existing,
                    record=expected_record,
                )
            )
            continue

        existing_record = existing_records.get(existing["id"]) if existing is not None else None
        if existing is None:
            status, action = "missing", "none"
        elif existing["archived"]:
            status, action = "already_archived", "none"
        elif existing_record == expected_record:
            if existing["id"] in active_linked_exclusion_ids:
                status, action = "linked_exclusions", "none"
                counts["conflict"] = counts.get("conflict", 0) + 1
                conflicts.append(
                    _conflict(
                        "retirement_conflict",
                        source_key,
                        "guarded offer has active linked exclusions",
                        offer_id=existing["id"],
                    )
                )
            else:
                status, action = "retire", "archive"
                safe_offer_ids.add(existing["id"])
        else:
            status, action = "changed", "none"
            counts["conflict"] = counts.get("conflict", 0) + 1
            conflicts.append(
                _conflict(
                    "retirement_conflict",
                    source_key,
                    "active offer differs from the expected retirement record",
                    offer_id=existing["id"],
                    expected_record=expected_record,
                    existing_record=existing_record,
                )
            )
        counts[status] = counts.get(status, 0) + 1
        operations.append(
            _retirement_operation(
                source_key,
                status,
                action,
                existing=existing,
                record=expected_record,
                existing_record=existing_record if status == "changed" else None,
            )
        )

    return operations, conflicts, counts, safe_offer_ids, collision_keys


def _manual_offer_retirement_operation(
    offer_id: int | None,
    status: str,
    action: str,
    *,
    expected_record: dict[str, Any] | None = None,
    existing: sqlite3.Row | None = None,
    existing_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "kind": "manual_offer_retirement",
        "source_key": None,
        "status": status,
        "action": action,
        "offer_id": offer_id,
        "brand_id": existing["brand_id"] if existing is not None else None,
        "record": expected_record,
    }
    if existing is not None:
        operation["existing_source_key"] = existing["source_key"]
        operation["existing_record"] = existing_record
    return operation


def _analyse_manual_offer_retirements(
    raw_items: list[Any],
    *,
    existing_by_id: dict[int, sqlite3.Row],
    existing_records: dict[int, dict[str, Any]],
    active_linked_exclusion_ids: set[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int], set[int]]:
    """Classify explicit source-less offer archives using immutable row guards."""

    operations: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    safe_offer_ids: set[int] = set()
    parsed: list[tuple[int, int, dict[str, Any]]] = []
    duplicate_ids: set[int] = set()
    seen_ids: set[int] = set()

    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            reason = f"manual_offer_retirements[{index}] must be an object"
            conflicts.append(_conflict("snapshot_problem", None, reason))
            operations.append(_manual_offer_retirement_operation(None, "snapshot_problem", "none"))
            continue
        try:
            offer_id = raw.get("offer_id")
            if isinstance(offer_id, bool) or not isinstance(offer_id, int) or offer_id < 1:
                raise PartnerBatchError(
                    f"manual_offer_retirements[{index}].offer_id must be a positive integer"
                )
            expected_record = _offer_guard_record(
                raw.get("expected_offer"), f"manual_offer_retirements[{index}].expected_offer"
            )
        except (PartnerBatchError, PartnerRewardError, TypeError, ValueError) as exc:
            conflicts.append(_conflict("snapshot_problem", None, str(exc)))
            operations.append(_manual_offer_retirement_operation(None, "snapshot_problem", "none"))
            continue
        if offer_id in seen_ids:
            duplicate_ids.add(offer_id)
        seen_ids.add(offer_id)
        parsed.append((index, offer_id, expected_record))

    for _index, offer_id, expected_record in parsed:
        if offer_id in duplicate_ids:
            reason = "offer_id appears more than once in manual_offer_retirements"
            conflicts.append(
                _conflict("manual_offer_retirement_conflict", None, reason, offer_id=offer_id)
            )
            operations.append(
                _manual_offer_retirement_operation(
                    offer_id, "duplicate", "none", expected_record=expected_record
                )
            )
            continue
        existing = existing_by_id.get(offer_id)
        existing_record = existing_records.get(offer_id) if existing is not None else None
        if existing is None:
            status, action = "missing", "none"
            conflicts.append(
                _conflict(
                    "manual_offer_retirement_conflict",
                    None,
                    "guarded manual offer does not exist",
                    offer_id=offer_id,
                )
            )
        elif existing["source_key"] is not None:
            status, action = "source_backed", "none"
            conflicts.append(
                _conflict(
                    "manual_offer_retirement_conflict",
                    existing["source_key"],
                    "guarded offer is source-backed, not source-less",
                    offer_id=offer_id,
                )
            )
        elif existing_record != expected_record:
            status, action = "changed", "none"
            conflicts.append(
                _conflict(
                    "manual_offer_retirement_conflict",
                    None,
                    "source-less offer differs from the immutable guard",
                    offer_id=offer_id,
                    expected_record=expected_record,
                    existing_record=existing_record,
                )
            )
        elif offer_id in active_linked_exclusion_ids:
            status, action = "linked_exclusions", "none"
            conflicts.append(
                _conflict(
                    "manual_offer_retirement_conflict",
                    None,
                    "guarded offer has active linked exclusions",
                    offer_id=offer_id,
                )
            )
        elif existing["archived"]:
            status, action = "already_archived", "none"
        else:
            status, action = "retire", "archive"
            safe_offer_ids.add(offer_id)
        counts[status] = counts.get(status, 0) + 1
        operations.append(
            _manual_offer_retirement_operation(
                offer_id,
                status,
                action,
                expected_record=expected_record,
                existing=existing,
                existing_record=existing_record,
            )
        )
    return operations, conflicts, counts, safe_offer_ids


def _seed_tombstone_operation(
    source_key: str | None,
    status: str,
    action: str,
    *,
    reason: str | None = None,
    offer_id: int | None = None,
    expected_record: dict[str, Any] | None = None,
    existing: sqlite3.Row | None = None,
    existing_tombstone_reason: str | None = None,
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "kind": "partner_seed_tombstone",
        "source_key": source_key,
        "status": status,
        "action": action,
        "reason": reason,
        "offer_id": offer_id,
        "record": expected_record,
        "existing_tombstone_reason": existing_tombstone_reason,
    }
    if existing is not None:
        operation["existing_offer_archived"] = bool(existing["archived"])
        operation["existing_offer_id"] = existing["id"]
    return operation


def _analyse_seed_tombstones(
    raw_items: list[Any],
    *,
    tombstone_rows: dict[str, sqlite3.Row] | None,
    existing_rows: dict[str, sqlite3.Row],
    existing_records: dict[int, dict[str, Any]],
    blocked_source_keys: set[str],
    active_linked_exclusion_ids: set[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int], set[int]]:
    """Classify explicit partner source tombstones and optional guarded archives."""

    operations: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    safe_offer_ids: set[int] = set()
    seen_keys: set[str] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            reason = f"partner_seed_tombstones[{index}] must be an object"
            conflicts.append(_conflict("snapshot_problem", None, reason))
            operations.append(_seed_tombstone_operation(None, "snapshot_problem", "none"))
            continue
        source_key: str | None = None
        try:
            source_key = _text(
                raw.get("source_key"),
                f"partner_seed_tombstones[{index}].source_key",
                required=True,
            )
            reason = _text(
                raw.get("reason"), f"partner_seed_tombstones[{index}].reason", required=True
            )
            has_offer_guard = "offer_id" in raw or "expected_offer" in raw
            offer_id: int | None = None
            expected_record: dict[str, Any] | None = None
            if has_offer_guard:
                if "offer_id" not in raw or "expected_offer" not in raw:
                    raise PartnerBatchError(
                        "partner_seed_tombstones guarded archives require "
                        "offer_id and expected_offer"
                    )
                value = raw["offer_id"]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise PartnerBatchError(
                        f"partner_seed_tombstones[{index}].offer_id must be a positive integer"
                    )
                offer_id = value
                expected_record = _offer_guard_record(
                    raw["expected_offer"], f"partner_seed_tombstones[{index}].expected_offer"
                )
        except (PartnerBatchError, PartnerRewardError, TypeError, ValueError) as exc:
            conflicts.append(_conflict("snapshot_problem", source_key, str(exc)))
            operations.append(_seed_tombstone_operation(source_key, "snapshot_problem", "none"))
            continue
        assert source_key is not None
        if source_key in seen_keys or source_key in blocked_source_keys:
            reason_text = (
                "source key appears more than once in partner_seed_tombstones"
                if source_key in seen_keys
                else "source key is used by multiple snapshot rows"
            )
            conflicts.append(_conflict("source_key_conflict", source_key, reason_text))
            operations.append(
                _seed_tombstone_operation(
                    source_key,
                    "source_key_conflict",
                    "none",
                    reason=reason,
                    offer_id=offer_id,
                    expected_record=expected_record,
                )
            )
            seen_keys.add(source_key)
            continue
        seen_keys.add(source_key)
        if tombstone_rows is None:
            reason_text = "partner_seed_tombstones table is missing"
            conflicts.append(_conflict("snapshot_problem", source_key, reason_text))
            operations.append(
                _seed_tombstone_operation(
                    source_key,
                    "snapshot_problem",
                    "none",
                    reason=reason,
                    offer_id=offer_id,
                    expected_record=expected_record,
                )
            )
            continue
        tombstone = tombstone_rows.get(source_key)
        existing = existing_rows.get(source_key)
        existing_record = existing_records.get(existing["id"]) if existing is not None else None
        if tombstone is not None and tombstone["reason"] != reason:
            status, action = "tombstone_changed", "none"
            conflicts.append(
                _conflict(
                    "tombstone_conflict",
                    source_key,
                    "existing partner source tombstone has a different reason",
                    existing_reason=tombstone["reason"],
                    expected_reason=reason,
                )
            )
        elif existing is not None and not existing["archived"]:
            guard_matches = (
                offer_id is not None
                and expected_record is not None
                and offer_id == existing["id"]
                and existing_record == expected_record
            )
            if not guard_matches:
                status, action = "active_offer_guard_conflict", "none"
                conflicts.append(
                    _conflict(
                        "tombstone_conflict",
                        source_key,
                        "active partner offer requires an exact archive guard",
                        offer_id=existing["id"],
                        expected_record=expected_record,
                        existing_record=existing_record,
                    )
                )
            elif existing["id"] in active_linked_exclusion_ids:
                status, action = "linked_exclusions", "none"
                conflicts.append(
                    _conflict(
                        "tombstone_conflict",
                        source_key,
                        "guarded offer has active linked exclusions",
                        offer_id=existing["id"],
                    )
                )
            else:
                status, action = "retire", "archive_and_tombstone"
                safe_offer_ids.add(existing["id"])
        elif existing is not None:
            guard_matches = offer_id is None or (
                offer_id == existing["id"]
                and expected_record is not None
                and existing_record == expected_record
            )
            if not guard_matches:
                status, action = "changed", "none"
                conflicts.append(
                    _conflict(
                        "tombstone_conflict",
                        source_key,
                        "archived partner offer differs from the optional guard",
                        offer_id=existing["id"],
                        expected_record=expected_record,
                        existing_record=existing_record,
                    )
                )
            elif offer_id is not None and existing["id"] in active_linked_exclusion_ids:
                status, action = "linked_exclusions", "none"
                conflicts.append(
                    _conflict(
                        "tombstone_conflict",
                        source_key,
                        "guarded offer has active linked exclusions",
                        offer_id=existing["id"],
                    )
                )
            elif tombstone is None:
                status, action = "already_archived", "tombstone"
            else:
                status, action = "already_tombstoned", "none"
        elif tombstone is None:
            if offer_id is not None:
                status, action = "missing_guarded_offer", "none"
                conflicts.append(
                    _conflict(
                        "tombstone_conflict",
                        source_key,
                        "guarded partner offer does not exist for the source key",
                        offer_id=offer_id,
                    )
                )
            else:
                status, action = "missing", "tombstone"
        else:
            if offer_id is not None:
                status, action = "missing_guarded_offer", "none"
                conflicts.append(
                    _conflict(
                        "tombstone_conflict",
                        source_key,
                        "guarded partner offer does not exist for the source key",
                        offer_id=offer_id,
                    )
                )
            else:
                status, action = "already_tombstoned", "none"
        counts[status] = counts.get(status, 0) + 1
        operations.append(
            _seed_tombstone_operation(
                source_key,
                status,
                action,
                reason=reason,
                offer_id=offer_id,
                expected_record=expected_record,
                existing=existing,
                existing_tombstone_reason=tombstone["reason"] if tombstone is not None else None,
            )
        )
    return operations, conflicts, counts, safe_offer_ids


def _mapping_repair_operation(
    source_key: str | None,
    status: str,
    action: str,
    *,
    offer_source_key: str | None = None,
    mapping_id: int | None = None,
    offer_id: int | None = None,
    expected_brand_id: int | None = None,
    target_brand_id: int | None = None,
    expected_record: dict[str, Any] | None = None,
    existing_record: dict[str, Any] | None = None,
    existing_mapping_brand_id: int | None = None,
) -> dict[str, Any]:
    return {
        "kind": "partner_mapping_repair",
        "source_key": source_key,
        "offer_source_key": offer_source_key,
        "status": status,
        "action": action,
        "mapping_id": mapping_id,
        "offer_id": offer_id,
        "expected_brand_id": expected_brand_id,
        "target_brand_id": target_brand_id,
        "record": expected_record,
        "existing_record": existing_record,
        "existing_mapping_brand_id": existing_mapping_brand_id,
    }


def _analyse_mapping_repairs(
    raw_items: list[Any],
    *,
    connection: sqlite3.Connection,
    mapping_rows: dict[str, sqlite3.Row],
    mapping_id_supported: bool,
    existing_by_id: dict[int, sqlite3.Row],
    existing_records: dict[int, dict[str, Any]],
    brands_by_id: dict[int, sqlite3.Row],
    blocked_source_keys: set[str],
    active_linked_exclusion_ids: set[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Plan one explicit archived-brand mapping and offer rebind at a time."""

    del connection  # Kept in the signature to make the read-only contract explicit.
    operations: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    seen_keys: set[str] = set()
    for index, raw in enumerate(raw_items):
        source_key: str | None = None
        offer_source_key: str | None = None
        expected_record: dict[str, Any] | None = None
        mapping_id: int | None = None
        offer_id: int | None = None
        expected_brand_id: int | None = None
        target_brand_id: int | None = None
        try:
            if not isinstance(raw, dict):
                raise PartnerBatchError(f"partner_mapping_repairs[{index}] must be an object")
            source_key = _text(
                raw.get("source_key"),
                f"partner_mapping_repairs[{index}].source_key",
                required=True,
            )
            offer_source_key = _text(
                raw.get("offer_source_key"),
                f"partner_mapping_repairs[{index}].offer_source_key",
                required=True,
            )
            for field in ("expected_brand_id", "target_brand_id", "offer_id"):
                value = raw.get(field)
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise PartnerBatchError(
                        f"partner_mapping_repairs[{index}].{field} must be a positive integer"
                    )
                if field == "expected_brand_id":
                    expected_brand_id = value
                elif field == "target_brand_id":
                    target_brand_id = value
                else:
                    offer_id = value
            expected_record = _offer_guard_record(
                raw.get("expected_offer"), f"partner_mapping_repairs[{index}].expected_offer"
            )
            if expected_record["brand_id"] != expected_brand_id:
                raise PartnerBatchError(
                    "partner_mapping_repairs.expected_offer.brand_id must equal expected_brand_id"
                )
            mapping_id = raw.get("mapping_id")
            if mapping_id is not None and (
                isinstance(mapping_id, bool) or not isinstance(mapping_id, int) or mapping_id < 1
            ):
                raise PartnerBatchError(
                    f"partner_mapping_repairs[{index}].mapping_id must be a positive integer"
                )
        except (PartnerBatchError, PartnerRewardError, TypeError, ValueError) as exc:
            conflicts.append(_conflict("snapshot_problem", source_key, str(exc)))
            operations.append(
                _mapping_repair_operation(
                    source_key,
                    "snapshot_problem",
                    "none",
                    offer_source_key=offer_source_key,
                    mapping_id=mapping_id,
                    offer_id=offer_id,
                    expected_brand_id=expected_brand_id,
                    target_brand_id=target_brand_id,
                    expected_record=expected_record,
                )
            )
            continue
        assert source_key is not None
        assert offer_source_key is not None
        assert offer_id is not None
        assert expected_brand_id is not None
        assert target_brand_id is not None
        if (
            source_key in seen_keys
            or source_key in blocked_source_keys
            or offer_source_key in blocked_source_keys
        ):
            reason = (
                "source key appears more than once in partner_mapping_repairs"
                if source_key in seen_keys
                else "source key is used by multiple snapshot rows"
            )
            conflicts.append(_conflict("source_key_conflict", source_key, reason))
            operations.append(
                _mapping_repair_operation(
                    source_key,
                    "source_key_conflict",
                    "none",
                    offer_source_key=offer_source_key,
                    mapping_id=mapping_id,
                    offer_id=offer_id,
                    expected_brand_id=expected_brand_id,
                    target_brand_id=target_brand_id,
                    expected_record=expected_record,
                )
            )
            seen_keys.add(source_key)
            continue
        seen_keys.add(source_key)
        if not mapping_id_supported:
            status = "schema_conflict"
            conflicts.append(
                _conflict(
                    "mapping_repair_conflict",
                    source_key,
                    "partner_seed_brands table has no stable id column for mapping repair",
                )
            )
            counts[status] = counts.get(status, 0) + 1
            operations.append(
                _mapping_repair_operation(
                    source_key,
                    status,
                    "none",
                    offer_source_key=offer_source_key,
                    mapping_id=mapping_id,
                    offer_id=offer_id,
                    expected_brand_id=expected_brand_id,
                    target_brand_id=target_brand_id,
                    expected_record=expected_record,
                )
            )
            continue
        mapping = mapping_rows.get(source_key)
        offer = existing_by_id.get(offer_id)
        source_brand = brands_by_id.get(expected_brand_id)
        target_brand = brands_by_id.get(target_brand_id)
        current_record = existing_records.get(offer_id) if offer is not None else None
        status, action = "missing", "none"
        reason: str | None = None
        if mapping is None:
            reason = "partner source mapping does not exist"
        elif mapping_id is not None and mapping["id"] != mapping_id:
            reason = "partner source mapping id changed"
        elif mapping["brand_id"] not in {expected_brand_id, target_brand_id}:
            reason = "partner source mapping points to an unexpected brand"
        elif source_brand is None or not source_brand["archived"]:
            reason = "source brand is not archived"
        elif source_brand["merged_into"] != target_brand_id:
            reason = "source brand does not directly merge into the target"
        elif (
            target_brand is None
            or target_brand["archived"]
            or target_brand["merged_into"] is not None
        ):
            reason = "target brand is not a direct active merge target"
        elif offer is None:
            reason = "guarded partner offer does not exist"
        elif offer["source_key"] != offer_source_key:
            reason = "guarded partner offer source key changed"
        elif offer["archived"]:
            reason = "guarded partner offer is archived"
        elif mapping["brand_id"] == expected_brand_id:
            if offer["brand_id"] != expected_brand_id:
                reason = "partner offer is not bound to the archived source brand"
            elif current_record != expected_record:
                reason = "partner offer differs from the immutable guard"
            elif offer_id in active_linked_exclusion_ids:
                status, action = "linked_exclusions", "none"
                conflicts.append(
                    _conflict(
                        "mapping_repair_conflict",
                        source_key,
                        "guarded offer has active linked exclusions",
                        offer_id=offer_id,
                    )
                )
            else:
                status, action = "repair", "repair"
        else:
            replay_record = dict(expected_record)
            replay_record["brand_id"] = target_brand_id
            if offer["brand_id"] == target_brand_id and current_record == replay_record:
                status, action = "already_repaired", "none"
            else:
                reason = "mapping and offer are in a partial or unexpected repair state"
        if reason is not None:
            status = "conflict"
            conflicts.append(
                _conflict(
                    "mapping_repair_conflict",
                    source_key,
                    reason,
                    offer_id=offer_id,
                    expected_record=expected_record,
                    existing_record=current_record,
                    expected_brand_id=expected_brand_id,
                    target_brand_id=target_brand_id,
                )
            )
        counts[status] = counts.get(status, 0) + 1
        operations.append(
            _mapping_repair_operation(
                source_key,
                status,
                action,
                offer_source_key=offer_source_key,
                mapping_id=mapping["id"] if mapping is not None else mapping_id,
                offer_id=offer_id,
                expected_brand_id=expected_brand_id,
                target_brand_id=target_brand_id,
                expected_record=expected_record,
                existing_record=current_record,
                existing_mapping_brand_id=mapping["brand_id"] if mapping is not None else None,
            )
        )
    return operations, conflicts, counts


def _operation_sort_key(item: dict[str, Any]) -> tuple[str, str, int]:
    """Sort plan operations without comparing invalid ``None`` source keys."""

    return (
        str(item.get("kind") or ""),
        str(item.get("source_key") or ""),
        int(item.get("offer_id") or 0),
    )


def _logical_signature(raw: dict[str, Any], record: dict[str, Any], *, kind: str) -> str:
    """Return a duplicate identity that preserves planned-brand boundaries."""

    brand_key = raw.get("brand_key")
    if (
        kind in {"offer", "exclusion"}
        and record.get("brand_id") is None
        and isinstance(brand_key, str)
    ):
        return _canonical({"planned_brand_key": brand_key.strip(), "record": record})
    return _canonical(record)


def _analyse_kind(
    stores: StoreRepository,
    connection: sqlite3.Connection,
    raw_items: list[Any],
    *,
    kind: str,
    tombstones: set[str],
    existing_rows: dict[str, sqlite3.Row],
    existing_records: dict[str, dict[str, Any]],
    existing_signatures: dict[str, set[str]],
    planned_brands: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int], dict[str, int]]:
    operations: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    valid: list[tuple[dict[str, Any], str, int | None, Any, dict[str, Any], str]] = []
    seen_source_keys: set[str] = set()
    duplicate_source_keys: set[str] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            counts["snapshot_problems"] = counts.get("snapshot_problems", 0) + 1
            conflicts.append(
                _conflict("snapshot_problem", None, f"{kind}[{index}] must be an object")
            )
            continue
        source_key = raw.get("source_key")
        try:
            source_key = _text(source_key, f"{kind}[{index}].source_key", required=True)
        except PartnerBatchError as exc:
            counts["snapshot_problems"] = counts.get("snapshot_problems", 0) + 1
            conflicts.append(_conflict("snapshot_problem", None, str(exc)))
            continue
        if source_key in seen_source_keys:
            duplicate_source_keys.add(source_key)
        seen_source_keys.add(source_key)
        try:
            mccs = _offer_mccs(raw) if kind == "offer" else None
            if kind == "offer":
                brand_id, mapping_status, mapping_reason = _resolve_brand(
                    stores, connection, raw, planned_brands
                )
                if mapping_status not in {"resolved", "planned_creation"}:
                    counts[mapping_status] = counts.get(mapping_status, 0) + 1
                    conflicts.append(
                        _conflict(mapping_status, source_key, mapping_reason or mapping_status)
                    )
                    continue
                payload, record = _offer_input(raw, brand_id)
            else:
                if raw.get("brand_key") is None and raw.get("brand_id") is None:
                    brand_id = None
                    mapping_status = "resolved"
                else:
                    brand_id, mapping_status, mapping_reason = _resolve_brand(
                        stores, connection, raw, planned_brands
                    )
                    if mapping_status not in {"resolved", "planned_creation"}:
                        counts[mapping_status] = counts.get(mapping_status, 0) + 1
                        conflicts.append(
                            _conflict(mapping_status, source_key, mapping_reason or mapping_status)
                        )
                        continue
                payload, record = _exclusion_input(raw, brand_id)
        except (PartnerBatchError, PartnerRewardError, KeyError, TypeError, ValueError) as exc:
            counts["snapshot_problems"] = counts.get("snapshot_problems", 0) + 1
            conflicts.append(_conflict("snapshot_problem", source_key, str(exc)))
            continue
        valid.append((raw, source_key, brand_id, payload, record, mapping_status))

    # Never allow two rows with one source identity to become competing insert
    # operations.  Keep the conflict key stable and withhold both rows.
    if duplicate_source_keys:
        counts["snapshot_problems"] = counts.get("snapshot_problems", 0) + len(
            duplicate_source_keys
        )
        for source_key in sorted(duplicate_source_keys):
            conflicts.append(
                _conflict(
                    "snapshot_problem",
                    source_key,
                    "source_key appears more than once in the snapshot",
                )
            )
        valid = [item for item in valid if item[1] not in duplicate_source_keys]

    signatures: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    for raw, source_key, _brand_id, _payload, record, _mapping_status in valid:
        signature = _logical_signature(raw, record, kind=kind)
        signatures[signature].append((source_key, source_key in existing_rows))

    for raw, source_key, brand_id, payload, record, mapping_status in valid:
        status = "new"
        existing = existing_rows.get(source_key)
        if source_key in tombstones:
            status = "tombstoned"
            counts[status] = counts.get(status, 0) + 1
            conflicts.append(
                _conflict("tombstoned", source_key, "source key is durably tombstoned")
            )
        elif existing is not None and existing["archived"]:
            status = "archived"
            counts[status] = counts.get(status, 0) + 1
            conflicts.append(_conflict("archived", source_key, "existing source row is archived"))
        elif existing is not None and existing_records[source_key] == record:
            status = "unchanged"
            counts[status] = counts.get(status, 0) + 1
        elif existing is not None:
            status = "changed"
            counts[status] = counts.get(status, 0) + 1
            conflicts.append(
                _conflict("changed", source_key, "same source key has different persisted content")
            )
        else:
            counts[status] = counts.get(status, 0) + 1
        signature = _logical_signature(raw, record, kind=kind)
        duplicate_sources = [key for key, _has_row in signatures[signature] if key != source_key]
        duplicate_sources.extend(sorted(existing_signatures.get(signature, set()) - {source_key}))
        duplicate_sources = sorted(set(duplicate_sources))
        if duplicate_sources:
            counts["logical_duplicates"] = counts.get("logical_duplicates", 0) + 1
            conflicts.append(
                _conflict(
                    "logical_duplicate",
                    source_key,
                    "same logical payload exists under another source key",
                    duplicate_source_keys=duplicate_sources,
                )
            )
            if status == "new":
                status = "logical_duplicate"
        operation = {
            "kind": kind,
            "source_key": source_key,
            "status": status,
            "action": "insert" if status == "new" else "none",
            "brand_id": brand_id,
            "brand_key": (raw.get("brand_key") if isinstance(raw.get("brand_key"), str) else None),
            "brand_creation": (
                {
                    key: planned_brands[raw["brand_key"]][key]
                    for key in ("name", "aliases", "channel")
                }
                if mapping_status == "planned_creation"
                else None
            ),
            "mccs": mccs,
            "record": record,
            "payload": payload,
        }
        operations.append(operation)
    return operations, conflicts, counts, {}


def _date_windows_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_start, right_start = left.get("starts_on"), right.get("starts_on")
    left_end, right_end = left.get("ends_on"), right.get("ends_on")
    start = (
        max(item for item in (left_start, right_start) if item is not None)
        if (left_start is not None or right_start is not None)
        else None
    )
    end = (
        min(item for item in (left_end, right_end) if item is not None)
        if (left_end is not None or right_end is not None)
        else None
    )
    return start is None or end is None or start <= end


def _channels_overlap(left: str, right: str) -> bool:
    return left == right or left == "any" or right == "any"


def _offer_overlap_dimensions(
    candidate: dict[str, Any],
    existing: dict[str, Any],
    *,
    source_key: str,
    existing_source_key: str | None,
) -> tuple[str, ...]:
    dimensions: list[str] = []
    if source_key != existing_source_key:
        dimensions.append("source_identity")
    if candidate.get("source_url") != existing.get("source_url"):
        dimensions.append("source_url")
    if candidate.get("conditions") != existing.get("conditions"):
        dimensions.append("conditions")
    if candidate.get("channel") != existing.get("channel"):
        dimensions.append("channel")
    if candidate.get("reward_kind") != existing.get("reward_kind"):
        dimensions.append("reward_kind")
    if candidate.get("mode") != existing.get("mode"):
        dimensions.append("mode")
    if candidate.get("tiers") != existing.get("tiers"):
        dimensions.append("rate_or_tiers")
    if candidate.get("starts_on") != existing.get("starts_on") or candidate.get(
        "ends_on"
    ) != existing.get("ends_on"):
        dimensions.append("effective_dates")
    return tuple(dimensions)


def _snapshot_offer_overlap_conflicts(
    operations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Hold distinct new offers that would compete within one snapshot."""

    candidates = [
        operation
        for operation in operations
        if operation["kind"] == "offer"
        and operation["status"] == "new"
        and (operation.get("brand_id") is not None or isinstance(operation.get("brand_key"), str))
    ]
    conflicts: list[dict[str, Any]] = []
    affected: set[str] = set()
    references_by_source: dict[str, set[str]] = defaultdict(set)
    dimensions_by_pair: dict[tuple[str, str], tuple[str, ...]] = {}
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            same_brand = (
                left.get("brand_id") is not None and left.get("brand_id") == right.get("brand_id")
            ) or (
                left.get("brand_id") is None
                and right.get("brand_id") is None
                and left.get("brand_key") is not None
                and left.get("brand_key") == right.get("brand_key")
            )
            if not same_brand:
                continue
            left_record, right_record = left["record"], right["record"]
            if left_record == right_record:
                # This is already classified as logical_duplicate when both
                # source identities carry the same persisted payload.
                continue
            if left_record["card_id"] != right_record["card_id"]:
                continue
            if not _channels_overlap(left_record["channel"], right_record["channel"]):
                continue
            if not _date_windows_overlap(left_record, right_record):
                continue
            left_key, right_key = left["source_key"], right["source_key"]
            pair = tuple(sorted((left_key, right_key)))
            dimensions = _offer_overlap_dimensions(
                left_record,
                right_record,
                source_key=left_key,
                existing_source_key=right_key,
            )
            dimensions_by_pair[pair] = dimensions
            affected.update((left_key, right_key))
            references_by_source[left_key].add(right_key)
            references_by_source[right_key].add(left_key)
    for operation in operations:
        source_key = operation["source_key"]
        if source_key not in affected:
            continue
        operation["status"] = "snapshot_logical_overlap"
        operation["action"] = "none"
        references = sorted(references_by_source[source_key])
        dimensions = sorted(
            {
                dimension
                for pair, pair_dimensions in dimensions_by_pair.items()
                if source_key in pair
                for dimension in pair_dimensions
            }
        )
        conflicts.append(
            _conflict(
                "snapshot_logical_overlap",
                source_key,
                "new offers overlap at runtime but have different payloads",
                references=references,
                dimensions=dimensions,
            )
        )
    return conflicts, len(affected)


def _offer_overlap_conflicts(
    operations: list[dict[str, Any]],
    existing_rows: list[sqlite3.Row],
    existing_records: dict[int, dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Hold new offers that overlap a different persisted offer identity."""

    conflicts: list[dict[str, Any]] = []
    overlapping = 0
    for operation in operations:
        if operation["kind"] != "offer" or operation["status"] != "new":
            continue
        brand_id = operation.get("brand_id")
        if brand_id is None:
            # A planned partner-only brand has no persisted ID yet, so there is
            # no existing offer belonging to it to compare in this pass.
            continue
        candidate = operation["record"]
        for row in existing_rows:
            if row["archived"] or row["id"] not in existing_records:
                continue
            existing_source_key = row["source_key"]
            if existing_source_key == operation["source_key"]:
                continue
            if row["brand_id"] != brand_id or row["card_id"] != candidate["card_id"]:
                continue
            existing = existing_records[row["id"]]
            if not _channels_overlap(candidate["channel"], existing["channel"]):
                continue
            if not _date_windows_overlap(candidate, existing):
                continue
            dimensions = _offer_overlap_dimensions(
                candidate,
                existing,
                source_key=operation["source_key"],
                existing_source_key=existing_source_key,
            )
            # An identical source-backed row is already reported by the
            # logical-duplicate check.  Manual/source-less rows still need an
            # explicit hold even when every payload field happens to match.
            if not dimensions or (
                dimensions == ("source_identity",) and existing_source_key is not None
            ):
                continue
            overlapping += 1
            conflicts.append(
                _conflict(
                    "logical_overlap",
                    operation["source_key"],
                    "new offer overlaps an existing partner offer",
                    existing_source_key=existing_source_key,
                    existing_offer_id=row["id"],
                    mcc_scope="all_brand_mccs",
                    dimensions=list(dimensions),
                )
            )
    return conflicts, overlapping


def _post_repair_overlap_conflicts(
    mapping_operations: list[dict[str, Any]],
    offer_operations: list[dict[str, Any]],
    existing_rows: list[sqlite3.Row],
    existing_records: dict[int, dict[str, Any]],
    safe_offer_ids: set[int],
) -> tuple[list[dict[str, Any]], int, int]:
    """Hold repairs which would collide after rebinding to their target brand.

    A mapping repair changes the offer's effective brand without changing its
    source identity or other runtime dimensions.  Compare that hypothetical
    post-repair record against persisted target-brand offers, planned inserts,
    and sibling repairs before approving either side of a collision.
    """

    repairs = [operation for operation in mapping_operations if operation["action"] == "repair"]
    planned = [operation for operation in offer_operations if operation["action"] == "insert"]
    conflicts: list[dict[str, Any]] = []
    affected_repairs: set[int] = set()
    affected_planned: set[str] = set()
    seen_pairs: set[tuple[str, str, str]] = set()

    def candidate(operation: dict[str, Any]) -> dict[str, Any]:
        record = dict(operation["record"])
        if operation["kind"] == "partner_mapping_repair":
            record["brand_id"] = operation["target_brand_id"]
        return record

    def report_pair(
        repair: dict[str, Any],
        other: dict[str, Any] | sqlite3.Row,
        other_record: dict[str, Any],
        *,
        other_kind: str,
    ) -> None:
        repair_key = str(repair.get("source_key") or "")
        if isinstance(other, sqlite3.Row):
            other_key = str(other["source_key"] or "")
            other_id = other["id"]
        else:
            other_key = str(other.get("source_key") or "")
            other_id = other.get("offer_id")
        pair_key = (repair_key, other_kind, other_key or str(other_id or ""))
        if pair_key in seen_pairs:
            return
        seen_pairs.add(pair_key)
        dimensions = _offer_overlap_dimensions(
            candidate(repair),
            other_record,
            source_key=repair_key,
            existing_source_key=other_key or None,
        )
        affected_repairs.add(id(repair))
        if other_kind == "planned_offer":
            affected_planned.add(other_key)
        details = {
            "offer_id": repair.get("offer_id"),
            "target_brand_id": repair.get("target_brand_id"),
            "conflict_with_kind": other_kind,
            "conflict_with_source_key": other_key or None,
            "conflict_with_offer_id": other_id,
            "dimensions": list(dimensions),
        }
        conflicts.append(
            _conflict(
                "mapping_repair_conflict",
                repair.get("source_key"),
                "mapping repair would duplicate or overlap an offer after rebinding",
                **details,
            )
        )
        if other_kind == "planned_offer":
            conflicts.append(
                _conflict(
                    "mapping_repair_conflict",
                    other.get("source_key"),  # type: ignore[union-attr]
                    "planned offer would duplicate or overlap a mapping repair",
                    **{
                        "offer_id": other.get("offer_id"),  # type: ignore[union-attr]
                        "target_brand_id": repair.get("target_brand_id"),
                        "conflict_with_kind": "partner_mapping_repair",
                        "conflict_with_source_key": repair.get("source_key"),
                        "conflict_with_offer_id": repair.get("offer_id"),
                        "dimensions": list(dimensions),
                    },
                )
            )

    for repair in repairs:
        target_brand_id = repair["target_brand_id"]
        repair_record = candidate(repair)
        for row in existing_rows:
            if (
                row["archived"]
                or row["id"] in safe_offer_ids
                or row["id"] == repair.get("offer_id")
                or row["brand_id"] != target_brand_id
                or row["card_id"] != repair_record["card_id"]
            ):
                continue
            other_record = existing_records.get(row["id"])
            if other_record is None:
                continue
            if not _channels_overlap(repair_record["channel"], other_record["channel"]):
                continue
            if not _date_windows_overlap(repair_record, other_record):
                continue
            report_pair(repair, row, other_record, other_kind="active_target_offer")

        for other in planned:
            if (
                other["source_key"] == repair.get("offer_source_key")
                or other.get("brand_id") != target_brand_id
            ):
                continue
            other_record = other["record"]
            if other_record["card_id"] != repair_record["card_id"]:
                continue
            if not _channels_overlap(repair_record["channel"], other_record["channel"]):
                continue
            if not _date_windows_overlap(repair_record, other_record):
                continue
            report_pair(repair, other, other_record, other_kind="planned_offer")

    for index, left in enumerate(repairs):
        left_record = candidate(left)
        for right in repairs[index + 1 :]:
            if left["target_brand_id"] != right["target_brand_id"]:
                continue
            right_record = candidate(right)
            if right_record["card_id"] != left_record["card_id"]:
                continue
            if not _channels_overlap(left_record["channel"], right_record["channel"]):
                continue
            if not _date_windows_overlap(left_record, right_record):
                continue
            report_pair(left, right, right_record, other_kind="mapping_repair")
            report_pair(right, left, left_record, other_kind="mapping_repair")

    for operation in mapping_operations:
        if id(operation) in affected_repairs:
            operation["status"] = "logical_overlap"
            operation["action"] = "none"
    for operation in offer_operations:
        if operation["source_key"] in affected_planned:
            operation["status"] = "mapping_repair_overlap"
            operation["action"] = "none"
    return conflicts, len(affected_repairs), len(affected_planned)


def _plan_payload(
    raw: dict[str, Any], connection: sqlite3.Connection, stores: StoreRepository
) -> dict[str, Any]:
    _require_schema(connection)
    tombstone_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='partner_seed_tombstones'"
    ).fetchone()
    tombstone_rows = (
        {
            row["source_key"]: row
            for row in connection.execute("SELECT * FROM partner_seed_tombstones")
        }
        if tombstone_table
        else None
    )
    tombstones = set(tombstone_rows) if tombstone_rows is not None else set()
    all_offer_rows = list(connection.execute("SELECT * FROM partner_offers"))
    existing_offers_by_id = {row["id"]: row for row in all_offer_rows}
    offer_rows = {row["source_key"]: row for row in all_offer_rows if row["source_key"] is not None}
    exclusion_rows = {
        row["source_key"]: row
        for row in connection.execute(
            "SELECT * FROM partner_exclusions WHERE source_key IS NOT NULL"
        )
    }
    offer_records_by_id = {row["id"]: _db_offer_record(connection, row) for row in all_offer_rows}
    offer_records = {key: offer_records_by_id[row["id"]] for key, row in offer_rows.items()}
    exclusion_records = {key: _db_exclusion_record(row) for key, row in exclusion_rows.items()}
    source_key_index = _snapshot_source_key_index(raw)
    source_key_conflict_entries = {
        key: entries for key, entries in source_key_index.items() if len(entries) > 1
    }
    source_key_collision_keys = set(source_key_conflict_entries)
    active_linked_exclusion_ids = _active_linked_exclusion_ids(connection)
    source_key_conflicts = [
        _conflict(
            "source_key_conflict",
            source_key,
            "source_key is used by multiple snapshot rows",
            references=[{"kind": kind, "index": index} for kind, index in entries],
        )
        for source_key, entries in sorted(source_key_conflict_entries.items())
    ]
    (
        retirement_ops,
        retirement_conflicts,
        retirement_counts,
        safe_retired_offer_ids,
        _retirement_collision_keys,
    ) = _analyse_retirements(
        raw.get("offer_retirements", []),
        existing_rows=offer_rows,
        existing_records=offer_records_by_id,
        new_offer_keys=_snapshot_source_keys(raw["offers"]),
        blocked_source_keys=source_key_collision_keys,
        active_linked_exclusion_ids=active_linked_exclusion_ids,
    )
    (
        manual_retirement_ops,
        manual_retirement_conflicts,
        manual_retirement_counts,
        safe_manual_offer_ids,
    ) = _analyse_manual_offer_retirements(
        raw.get("manual_offer_retirements", []),
        existing_by_id=existing_offers_by_id,
        existing_records=offer_records_by_id,
        active_linked_exclusion_ids=active_linked_exclusion_ids,
    )
    (
        tombstone_ops,
        tombstone_conflicts,
        tombstone_counts,
        safe_tombstone_offer_ids,
    ) = _analyse_seed_tombstones(
        raw.get("partner_seed_tombstones", []),
        tombstone_rows=tombstone_rows,
        existing_rows=offer_rows,
        existing_records=offer_records_by_id,
        blocked_source_keys=source_key_collision_keys,
        active_linked_exclusion_ids=active_linked_exclusion_ids,
    )
    mapping_columns = _table_columns(connection, "partner_seed_brands")
    mapping_rows = {
        row["source_key"]: row for row in connection.execute("SELECT * FROM partner_seed_brands")
    }
    brand_rows = {row["id"]: row for row in connection.execute("SELECT * FROM store_brands")}
    mapping_repair_ops, mapping_repair_conflicts, mapping_repair_counts = _analyse_mapping_repairs(
        raw.get("partner_mapping_repairs", []),
        connection=connection,
        mapping_rows=mapping_rows,
        mapping_id_supported="id" in mapping_columns,
        existing_by_id=existing_offers_by_id,
        existing_records=offer_records_by_id,
        brands_by_id=brand_rows,
        blocked_source_keys=source_key_collision_keys,
        active_linked_exclusion_ids=active_linked_exclusion_ids,
    )
    safe_retired_offer_ids.update(safe_manual_offer_ids)
    safe_retired_offer_ids.update(safe_tombstone_offer_ids)
    offer_existing_signatures: dict[str, set[str]] = defaultdict(set)
    for key, row in offer_rows.items():
        if not row["archived"] and row["id"] not in safe_retired_offer_ids:
            offer_existing_signatures[_canonical(offer_records[key])].add(key)
    exclusion_existing_signatures: dict[str, set[str]] = defaultdict(set)
    for key, row in exclusion_rows.items():
        if not row["archived"]:
            exclusion_existing_signatures[_canonical(exclusion_records[key])].add(key)
    planned_brands = _planned_brand_context(raw["offers"] + raw["exclusions"])
    offer_ops, offer_conflicts, offer_counts, _ = _analyse_kind(
        stores,
        connection,
        raw["offers"],
        kind="offer",
        tombstones=tombstones,
        existing_rows=offer_rows,
        existing_records=offer_records,
        existing_signatures=offer_existing_signatures,
        planned_brands=planned_brands,
    )
    exclusion_ops, exclusion_conflicts, exclusion_counts, _ = _analyse_kind(
        stores,
        connection,
        raw["exclusions"],
        kind="exclusion",
        tombstones=tombstones,
        existing_rows=exclusion_rows,
        existing_records=exclusion_records,
        existing_signatures=exclusion_existing_signatures,
        planned_brands=planned_brands,
    )
    for operations_for_kind in (offer_ops, exclusion_ops):
        for operation in operations_for_kind:
            if operation["source_key"] in source_key_collision_keys:
                operation["status"] = "source_key_conflict"
                operation["action"] = "none"
    snapshot_overlap_conflicts, snapshot_overlap_count = _snapshot_offer_overlap_conflicts(
        offer_ops
    )
    offer_counts["snapshot_logical_overlaps"] = snapshot_overlap_count
    overlap_conflicts, overlap_count = _offer_overlap_conflicts(
        offer_ops,
        [row for row in all_offer_rows if row["id"] not in safe_retired_offer_ids],
        offer_records_by_id,
    )
    for operation in offer_ops:
        if any(item["source_key"] == operation["source_key"] for item in overlap_conflicts):
            operation["status"] = "logical_overlap"
            operation["action"] = "none"
    offer_counts["logical_overlaps"] = overlap_count
    (
        mapping_overlap_conflicts,
        mapping_overlap_repairs,
        mapping_overlap_planned,
    ) = _post_repair_overlap_conflicts(
        mapping_repair_ops,
        offer_ops,
        all_offer_rows,
        offer_records_by_id,
        safe_retired_offer_ids,
    )
    if mapping_overlap_repairs:
        mapping_repair_counts["repair"] = max(
            0, mapping_repair_counts.get("repair", 0) - mapping_overlap_repairs
        )
        mapping_repair_counts["logical_overlap"] = (
            mapping_repair_counts.get("logical_overlap", 0) + mapping_overlap_repairs
        )
    if mapping_overlap_planned:
        offer_counts["mapping_repair_overlaps"] = mapping_overlap_planned
    conflicts = (
        source_key_conflicts
        + retirement_conflicts
        + manual_retirement_conflicts
        + tombstone_conflicts
        + mapping_repair_conflicts
        + offer_conflicts
        + exclusion_conflicts
        + snapshot_overlap_conflicts
        + overlap_conflicts
        + mapping_overlap_conflicts
    )
    for problem in raw.get("problems", []):
        if isinstance(problem, dict):
            source_key = problem.get("source_key")
            reason = problem.get("reason", problem.get("message", _canonical(problem)))
        else:
            source_key = None
            reason = str(problem)
        conflicts.append(_conflict("snapshot_problem", source_key, str(reason)))
    counts = _base_counts()
    for prefix, values in (("offers", offer_counts), ("exclusions", exclusion_counts)):
        for key, value in values.items():
            counts[f"{prefix}_{key}"] = value
        counts[f"{prefix}_mapping_conflicts"] = counts[f"{prefix}_mapping_conflict"]
    for key, value in retirement_counts.items():
        counts[f"offer_retirements_{key}"] = value
    for key, value in manual_retirement_counts.items():
        counts[f"manual_offer_retirements_{key}"] = value
    for key, value in tombstone_counts.items():
        counts[f"partner_seed_tombstones_{key}"] = value
    for key, value in mapping_repair_counts.items():
        counts[f"partner_mapping_repairs_{key}"] = value
    partner_operations = (
        retirement_ops
        + manual_retirement_ops
        + tombstone_ops
        + mapping_repair_ops
        + offer_ops
        + exclusion_ops
    )
    mapped_keys = {
        row["source_key"]
        for row in connection.execute("SELECT source_key FROM partner_seed_brands")
    }
    approved_partner_operations = [
        operation for operation in (offer_ops + exclusion_ops) if operation["action"] == "insert"
    ]
    planned_keys = {
        operation["brand_key"]
        for operation in approved_partner_operations
        if operation.get("brand_creation") is not None
    }
    brand_operations = [
        {
            "kind": "brand",
            "source_key": brand_key,
            "status": "new",
            "action": "insert",
            "brand_id": None,
            "brand_key": brand_key,
            "record": {
                "name": planned_brands[brand_key]["name"],
                "aliases": planned_brands[brand_key]["aliases"],
                "channel": planned_brands[brand_key]["channel"],
            },
        }
        for brand_key in sorted(planned_keys)
    ]
    # A new stable key is recorded together with its resolved brand.  This is
    # insert-only and makes a later preview reuse the exact same identity.
    mapping_targets: dict[str, int | None] = {}
    for operation in approved_partner_operations:
        brand_key = operation.get("brand_key")
        if (
            isinstance(brand_key, str)
            and brand_key not in mapped_keys
            and operation.get("brand_id") is not None
        ):
            mapping_targets.setdefault(brand_key, operation["brand_id"])
    mapping_targets.update({brand_key: None for brand_key in planned_keys})
    mapping_operations = [
        {
            "kind": "brand_mapping",
            "source_key": brand_key,
            "status": "new",
            "action": "insert",
            "brand_id": target,
            "brand_key": brand_key,
            "record": {"brand_key": brand_key, "brand_id": target},
        }
        for brand_key, target in sorted(mapping_targets.items())
    ]
    approved_tombstone_operations = [
        operation
        for operation in tombstone_ops
        if operation["action"] in {"tombstone", "archive_and_tombstone"}
        and operation.get("existing_tombstone_reason") is None
    ]
    approved_repair_operations = [
        operation for operation in mapping_repair_ops if operation["action"] == "repair"
    ]
    counts["brands_new"] = len(brand_operations)
    counts["brand_mappings_new"] = len(mapping_operations)
    approved = approved_partner_operations + brand_operations + mapping_operations
    approved_archives = [
        operation
        for operation in (retirement_ops + manual_retirement_ops + tombstone_ops)
        if operation["action"] in {"archive", "archive_and_tombstone"}
    ]
    counts["approved_archives"] = len(approved_archives)
    counts["partner_seed_tombstones_new"] = len(approved_tombstone_operations)
    counts["approved_repairs"] = len(approved_repair_operations)
    counts["approved_inserts"] = len(approved) + len(approved_tombstone_operations)
    counts["approved_operations"] = (
        counts["approved_inserts"] + counts["approved_archives"] + counts["approved_repairs"]
    )
    counts["approved_partner_rows"] = len(approved_partner_operations)
    counts["source_key_conflicts"] = len(source_key_conflicts)
    counts["snapshot_problems"] = len(raw.get("problems", []))
    counts["conflicts"] = len(conflicts)
    # Payload objects are converted into stable records for JSON output and apply.
    operations = []
    for operation in brand_operations + mapping_operations + partner_operations:
        item = {key: value for key, value in operation.items() if key != "payload"}
        operations.append(item)
    operations.sort(key=_operation_sort_key)
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "snapshot_sha256": _snapshot_sha(raw),
        "database_fingerprint": _database_fingerprint(connection),
        "counts": counts,
        "operations": operations,
        "conflicts": sorted(
            conflicts,
            key=lambda item: (item["kind"], item.get("source_key") or "", item["reason"]),
        ),
        "source": {
            "offers": len(raw["offers"]),
            "exclusions": len(raw["exclusions"]),
            "offer_retirements": len(raw.get("offer_retirements", [])),
            "manual_offer_retirements": len(raw.get("manual_offer_retirements", [])),
            "partner_mapping_repairs": len(raw.get("partner_mapping_repairs", [])),
            "partner_seed_tombstones": len(raw.get("partner_seed_tombstones", [])),
            "problems": len(raw.get("problems", [])),
        },
    }


def preview_partner_batch(
    stores_or_path: StoreRepository | Path | str,
    snapshot: Path | str | dict[str, Any],
) -> dict[str, Any]:
    """Return a deterministic, read-only approval plan for ``snapshot``."""

    stores = (
        stores_or_path
        if isinstance(stores_or_path, StoreRepository)
        else StoreRepository(stores_or_path)
    )
    raw = load_snapshot(snapshot)
    with _read_connection(stores) as connection:
        payload = _plan_payload(raw, connection, stores)
    plan_sha = _sha256(_canonical(payload))
    return {**payload, "plan_sha256": plan_sha}


def _apply_mapping_repair(
    partners: PartnerRepository,
    connection: sqlite3.Connection,
    operation: dict[str, Any],
    *,
    actor_id: int,
) -> None:
    """Apply one exact mapping/offer rebind inside the caller transaction."""

    source_key = operation.get("source_key")
    offer_source_key = operation.get("offer_source_key")
    mapping_id = operation.get("mapping_id")
    offer_id = operation.get("offer_id")
    expected_brand_id = operation.get("expected_brand_id")
    target_brand_id = operation.get("target_brand_id")
    expected_record = operation.get("record")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        for value in (mapping_id, offer_id, expected_brand_id, target_brand_id)
    ):
        raise StalePartnerPlanError("Mapping repair guard is incomplete")
    if not isinstance(source_key, str) or not isinstance(offer_source_key, str):
        raise StalePartnerPlanError("Mapping repair source guard is incomplete")
    if "id" not in _table_columns(connection, "partner_seed_brands"):
        raise StalePartnerPlanError("Partner source mapping has no stable id column")
    mapping = connection.execute(
        "SELECT * FROM partner_seed_brands WHERE id=? AND source_key=?",
        (mapping_id, source_key),
    ).fetchone()
    if mapping is None or mapping["brand_id"] != expected_brand_id:
        raise StalePartnerPlanError("Partner source mapping changed before repair")
    source_brand = connection.execute(
        "SELECT archived,merged_into FROM store_brands WHERE id=?", (expected_brand_id,)
    ).fetchone()
    target_brand = connection.execute(
        "SELECT archived,merged_into FROM store_brands WHERE id=?", (target_brand_id,)
    ).fetchone()
    if (
        source_brand is None
        or not source_brand["archived"]
        or source_brand["merged_into"] != target_brand_id
        or target_brand is None
        or target_brand["archived"]
        or target_brand["merged_into"] is not None
    ):
        raise StalePartnerPlanError("Brand merge guard changed before repair")
    offer = connection.execute(
        "SELECT * FROM partner_offers WHERE id=? AND source_key=?",
        (offer_id, offer_source_key),
    ).fetchone()
    if offer is None or offer["archived"] or offer["brand_id"] != expected_brand_id:
        raise StalePartnerPlanError("Partner offer guard changed before repair")
    if offer_id in _active_linked_exclusion_ids(connection):
        raise StalePartnerPlanError("Partner offer has active linked exclusions")
    current_record = _db_offer_record(connection, offer)
    if current_record != expected_record:
        raise StalePartnerPlanError("Partner offer content changed before repair")
    mapping_update = connection.execute(
        "UPDATE partner_seed_brands SET brand_id=? WHERE id=? AND source_key=? AND brand_id=?",
        (target_brand_id, mapping_id, source_key, expected_brand_id),
    )
    if mapping_update.rowcount != 1:
        raise StalePartnerPlanError("Partner source mapping changed before repair")
    partners._audit(
        connection,
        "brand_mapping",
        mapping_id,
        "rebind",
        actor_id,
        {
            "source_key": source_key,
            "before": {"brand_id": expected_brand_id},
            "after": {"brand_id": target_brand_id},
        },
    )
    connection.execute(
        "UPDATE partner_offers SET brand_id=?,updated_by=?,updated_at=CURRENT_TIMESTAMP "
        "WHERE id=? AND source_key=? AND brand_id=? AND archived=0",
        (target_brand_id, actor_id, offer_id, offer_source_key, expected_brand_id),
    )
    updated = connection.execute("SELECT * FROM partner_offers WHERE id=?", (offer_id,)).fetchone()
    if updated is None or updated["brand_id"] != target_brand_id:
        raise StalePartnerPlanError("Partner offer changed before repair")
    after_record = _db_offer_record(connection, updated)
    partners._audit(
        connection,
        "offer",
        offer_id,
        "rebind",
        actor_id,
        {"before": current_record, "after": after_record},
    )


def _backup_database(
    stores: StoreRepository,
    source: sqlite3.Connection,
    plan_sha256: str,
    requested: Path | str | None,
) -> Path:
    if requested is None:
        if getattr(stores, "_memory", False):
            raise PartnerBatchError("--backup is required for an in-memory database")
        path = stores.path.with_name(f"{stores.path.name}.partner-batch-{plan_sha256[:12]}.bak")
        counter = 1
        while path.exists():
            path = stores.path.with_name(
                f"{stores.path.name}.partner-batch-{plan_sha256[:12]}-{counter}.bak"
            )
            counter += 1
    else:
        path = Path(requested)
    path = path.resolve()
    if not getattr(stores, "_memory", False) and path == stores.path.resolve():
        raise PartnerBatchError("Backup path must differ from database path")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise PartnerBatchError(f"Backup path already exists: {path}")
    remove_on_failure = not path.exists()
    destination: sqlite3.Connection | None = None
    try:
        destination = sqlite3.connect(path)
        destination.execute("PRAGMA foreign_keys=ON")
        source.backup(destination)
        destination.commit()
        quick = destination.execute("PRAGMA quick_check").fetchone()[0]
        if quick != "ok":
            raise PartnerBatchError(f"Backup SQLite quick_check failed: {quick}")
        foreign = destination.execute("PRAGMA foreign_key_check").fetchall()
        if foreign:
            raise PartnerBatchError("Backup SQLite foreign_key_check failed")
        destination.close()
        destination = None
    except BaseException:
        if destination is not None:
            with suppress(sqlite3.Error):
                destination.rollback()
            with suppress(sqlite3.Error):
                destination.close()
        if remove_on_failure and path.exists():
            try:
                path.unlink()
            except OSError as exc:
                raise PartnerBatchError(f"Unable to remove incomplete backup: {path}") from exc
        raise
    return path


def apply_partner_batch(
    stores_or_path: StoreRepository | Path | str,
    snapshot: Path | str | dict[str, Any],
    *,
    actor_id: int,
    expected_plan_sha256: str,
    backup_path: Path | str | None = None,
) -> dict[str, Any]:
    """Apply approved inserts iff the target still exactly matches preview."""

    if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id < 1:
        raise PartnerBatchError("actor_id must be a positive integer")
    expected = _text(expected_plan_sha256, "expected_plan_sha256", required=True).lower()
    stores = (
        stores_or_path
        if isinstance(stores_or_path, StoreRepository)
        else StoreRepository(stores_or_path)
    )
    raw = load_snapshot(snapshot)
    fresh = preview_partner_batch(stores, raw)
    if fresh["plan_sha256"].lower() != expected:
        raise StalePartnerPlanError(
            "Approval token is stale: preview the unchanged snapshot and database again"
        )
    partners = PartnerRepository(stores)
    # A read connection is used for the backup; BEGIN IMMEDIATE then closes the
    # race and checks the same fingerprint again before inserting anything.
    with _read_connection(stores) as source:
        backup = _backup_database(stores, source, fresh["plan_sha256"], backup_path)
    inserted = {"offers": 0, "exclusions": 0}
    archived = {"offers": 0}
    repaired = {"mappings": 0, "offers": 0}
    tombstoned = 0
    created_brands: dict[str, int] = {}
    operation_rows = _snapshot_operation_rows(raw)
    with stores.transaction() as connection:
        if _database_fingerprint(connection) != fresh["database_fingerprint"]:
            raise StalePartnerPlanError("Database changed after preview")
        # Reuse the exact validated source records from a fresh analysis.  The
        # source rows are looked up again so moderator edits are never overwritten.
        current = _plan_payload(raw, connection, stores)
        if _sha256(_canonical(current)) != fresh["plan_sha256"]:
            raise StalePartnerPlanError("Database or snapshot changed before apply")
        ordered_operations = sorted(current["operations"], key=_operation_sort_key)
        # Retirements are deliberately applied before any replacement creates;
        # the surrounding transaction rolls the archive back if a later write
        # fails.
        for operation in ordered_operations:
            if operation["action"] not in {"archive", "archive_and_tombstone"}:
                continue
            if operation["kind"] not in {
                "offer_retirement",
                "manual_offer_retirement",
                "partner_seed_tombstone",
            }:
                continue
            offer_id = operation.get("offer_id")
            if offer_id in _active_linked_exclusion_ids(connection):
                raise StalePartnerPlanError("Offer has active linked exclusions")
            if offer_id is None or not partners.delete_offer(
                offer_id,
                actor_id=actor_id,
                connection=connection,
            ):
                raise StalePartnerPlanError("Offer changed before retirement")
            archived["offers"] += 1
        for operation in ordered_operations:
            if operation["kind"] != "partner_mapping_repair" or operation["action"] != "repair":
                continue
            _apply_mapping_repair(partners, connection, operation, actor_id=actor_id)
            repaired["mappings"] += 1
            repaired["offers"] += 1
        for operation in ordered_operations:
            if operation["kind"] in {
                "offer_retirement",
                "manual_offer_retirement",
                "partner_mapping_repair",
            }:
                continue
            if operation["action"] != "insert" and not (
                operation["kind"] == "partner_seed_tombstone"
                and operation["action"] in {"tombstone", "archive_and_tombstone"}
            ):
                continue
            source_key = operation["source_key"]
            if operation["kind"] == "partner_seed_tombstone":
                existing_tombstone = connection.execute(
                    "SELECT reason FROM partner_seed_tombstones WHERE source_key=?", (source_key,)
                ).fetchone()
                if existing_tombstone is None:
                    connection.execute(
                        "INSERT INTO partner_seed_tombstones(source_key,reason) VALUES(?,?)",
                        (source_key, operation.get("reason")),
                    )
                    tombstoned += 1
                elif existing_tombstone["reason"] != operation.get("reason"):
                    raise StalePartnerPlanError("Partner source tombstone changed before apply")
                continue
            if operation["kind"] == "brand":
                record = operation["record"]
                result = stores.apply_change(
                    "add_merchant",
                    {
                        "name": record["name"],
                        "aliases": record["aliases"],
                        "channel": record["channel"],
                    },
                    actor_id,
                    connection=connection,
                )
                if result.brand_id is None:
                    raise PartnerBatchError("Created partner-only brand has no brand id")
                created_brands[source_key] = result.brand_id
                continue
            if operation["kind"] == "brand_mapping":
                brand_id = operation.get("brand_id") or created_brands.get(source_key)
                if brand_id is None or not _active_brand(connection, brand_id):
                    raise PartnerBatchError("Cannot create partner brand mapping")
                try:
                    connection.execute(
                        "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?)",
                        (source_key, brand_id),
                    )
                except sqlite3.IntegrityError as exc:
                    raise StalePartnerPlanError("Brand mapping changed after preview") from exc
                continue
            # A plan contains records, not executable Python objects; rebuild
            # validated inputs from the snapshot row before calling repository
            # APIs, which also supplies the existing audit contract.
            raw_item = operation_rows.get((operation["kind"], source_key))
            if raw_item is None:
                raise StalePartnerPlanError("Snapshot row missing before apply")
            brand_id = operation.get("brand_id") or created_brands.get(operation.get("brand_key"))
            if brand_id is None and operation["kind"] == "offer":
                raise PartnerBatchError("Offer has no resolved brand")
            if operation["kind"] == "offer":
                payload, _record = _offer_input(raw_item, brand_id)
                partners.create_offer(
                    payload,
                    actor_id=actor_id,
                    connection=connection,
                    source_key=source_key,
                )
                inserted["offers"] += 1
            else:
                payload, _record = _exclusion_input(raw_item, brand_id)
                partners.create_exclusion(
                    payload,
                    actor_id=actor_id,
                    connection=connection,
                    source_key=source_key,
                )
                inserted["exclusions"] += 1
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick != "ok":
            raise PartnerBatchError(f"SQLite quick_check failed: {quick}")
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign:
            raise PartnerBatchError("SQLite foreign_key_check failed")
    return {
        "plan_sha256": fresh["plan_sha256"],
        "backup_path": str(backup),
        "inserted": inserted,
        "archived": archived,
        "repaired": repaired,
        "tombstoned": tombstoned,
        "counts": fresh["counts"],
        "database_fingerprint": fresh["database_fingerprint"],
    }


# Short aliases make the library entry points easy to discover in tests/tools.
preview_batch = preview_partner_batch
apply_batch = apply_partner_batch


def main(argv: list[str] | None = None) -> None:
    """Preview a local snapshot, or apply it with an explicit approval token."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("MCC_STORES_PATH", "var/stores.sqlite3")),
    )
    parser.add_argument("--snapshot", "--seed", dest="snapshot", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--backup", type=Path)
    parser.add_argument(
        "--actor-id", type=int, default=int(os.environ.get("BOT_OWNER_TELEGRAM_ID", "0"))
    )
    args = parser.parse_args(argv)
    if args.apply:
        if not args.expected_plan_sha256:
            parser.error("--expected-plan-sha256 is required with --apply")
        result = apply_partner_batch(
            args.database,
            args.snapshot,
            actor_id=args.actor_id,
            expected_plan_sha256=args.expected_plan_sha256,
            backup_path=args.backup,
        )
    else:
        result = preview_partner_batch(args.database, args.snapshot)
    _write_json_result(result)


if __name__ == "__main__":
    main()
