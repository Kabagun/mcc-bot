"""Apply the reviewed 2026-09-01 manual store locations fail-closed.

Imported addresses remain provenance in ``store_sources``.  This manifest is
the only bridge from that provenance to the user-maintained
``store_brands.location`` field: every row names an exact brand snapshot and
an exact source fingerprint, and the whole batch is applied transactionally.
"""

# Reviewed merchant names and addresses intentionally contain mixed scripts.
# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .stores import StoreError, StoreRepository

MANIFEST_PATH = Path(__file__).with_name("data") / "location_review_20260901.json"
EXPECTED_BRAND_IDS = (
    44,
    46,
    55,
    119,
    135,
    185,
    193,
    194,
    196,
    293,
    301,
    303,
    316,
    324,
    394,
    425,
    426,
    445,
    513,
    543,
    568,
    583,
    601,
    657,
    667,
    675,
    694,
    702,
    705,
    749,
    768,
    788,
    791,
    828,
    841,
    870,
    886,
    920,
    1025,
)
EXPECTED_SOURCE_ADDRESS_ROWS = 1353
EXPECTED_ADDRESS_BEARING_ACTIVE_BRANDS = 759


class LocationReviewError(RuntimeError):
    """Raised when the reviewed database snapshot no longer matches."""


@dataclass(frozen=True, slots=True)
class LocationReviewEntry:
    """One exact reviewed brand state and its desired manual location."""

    brand_id: int
    expected_name: str
    expected_revision: int
    expected_location: str | None
    desired_location: str | None
    source_fingerprint: str
    decision: str


def load_manifest(path: Path = MANIFEST_PATH) -> tuple[LocationReviewEntry, ...]:
    """Load and validate the immutable reviewed-location manifest."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocationReviewError(f"Не удалось прочитать manifest адресов: {exc}") from exc
    if raw.get("manifest") != "manual-store-locations-2026-09-01":
        raise LocationReviewError("Неизвестная версия manifest адресов")
    review = raw.get("review")
    if not isinstance(review, dict) or (
        review.get("source_address_rows") != EXPECTED_SOURCE_ADDRESS_ROWS
        or review.get("address_bearing_active_brands") != EXPECTED_ADDRESS_BEARING_ACTIVE_BRANDS
        or review.get("expected_entry_count") != len(EXPECTED_BRAND_IDS)
        or tuple(review.get("expected_brand_ids", ())) != EXPECTED_BRAND_IDS
        or review.get("explicit_network_without_location") != [1025]
    ):
        raise LocationReviewError("Состав или счётчики manifest адресов изменены")
    entries = []
    seen: set[int] = set()
    for item in raw.get("entries", []):
        try:
            entry = LocationReviewEntry(**item)
        except (TypeError, ValueError) as exc:
            raise LocationReviewError("Некорректная запись manifest адресов") from exc
        if entry.brand_id < 1 or entry.expected_revision < 1:
            raise LocationReviewError("ID и revision в manifest должны быть положительными")
        if entry.brand_id in seen:
            raise LocationReviewError(f"Повтор brand ID {entry.brand_id} в manifest")
        if len(entry.source_fingerprint) != 64:
            raise LocationReviewError(
                f"Некорректный source fingerprint для brand ID {entry.brand_id}"
            )
        seen.add(entry.brand_id)
        entries.append(entry)
    if tuple(entry.brand_id for entry in entries) != EXPECTED_BRAND_IDS:
        raise LocationReviewError("Набор brand ID в manifest адресов неполон или изменён")
    return tuple(entries)


def _source_rows(connection: Any, brand_id: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT s.id,s.source,s.store_id,s.merchant_id,s.network_id,s.metadata_json,
                  m.name AS merchant_name,m.channel,m.aliases_json,m.archived,
                  m.revision,m.source_identity
           FROM store_sources s
           JOIN store_brand_members bm ON bm.merchant_id=s.merchant_id
           JOIN store_merchants m ON m.id=s.merchant_id
           WHERE bm.brand_id=? ORDER BY s.id""",
        (brand_id,),
    ).fetchall()
    result = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"])
            aliases = json.loads(row["aliases_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise LocationReviewError(f"Некорректный JSON источника brand ID {brand_id}") from exc
        result.append(
            {
                "id": row["id"],
                "source": row["source"],
                "store_id": row["store_id"],
                "merchant_id": row["merchant_id"],
                "network_id": row["network_id"],
                "metadata": metadata,
                "merchant_name": row["merchant_name"],
                "channel": row["channel"],
                "aliases": aliases,
                "archived": row["archived"],
                "revision": row["revision"],
                "source_identity": row["source_identity"],
            }
        )
    return result


def source_fingerprint(connection: Any, brand_id: int) -> str:
    """Hash the reviewed source IDs, network metadata and imported addresses."""

    payload = json.dumps(
        _source_rows(connection, brand_id),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _validate_schema(connection: Any) -> None:
    required = {
        "store_brands": {"id", "name", "location", "archived", "revision", "merged_into"},
        "store_merchants": {
            "id",
            "name",
            "channel",
            "aliases_json",
            "archived",
            "revision",
            "source_identity",
        },
        "store_brand_members": {"brand_id", "merchant_id"},
        "store_sources": {
            "id",
            "source",
            "store_id",
            "merchant_id",
            "network_id",
            "metadata_json",
        },
    }
    for table, columns in required.items():
        actual = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not columns <= actual:
            raise LocationReviewError(
                f"Схема {table} не готова к manifest; сначала выполните additive-миграции"
            )


def _classify_entry(connection: Any, entry: LocationReviewEntry) -> str:
    row = connection.execute(
        """SELECT id,name,aliases_json,location,archived,revision,merged_into
           FROM store_brands WHERE id=?""",
        (entry.brand_id,),
    ).fetchone()
    if row is None:
        raise LocationReviewError(f"Brand ID {entry.brand_id} исчез")
    if row["name"] != entry.expected_name or row["archived"] or row["merged_into"] is not None:
        raise LocationReviewError(
            f"Brand ID {entry.brand_id} больше не соответствует «{entry.expected_name}»"
        )
    actual_fingerprint = source_fingerprint(connection, entry.brand_id)
    if actual_fingerprint != entry.source_fingerprint:
        raise LocationReviewError(f"Источники brand ID {entry.brand_id} изменились после проверки")
    if row["revision"] == entry.expected_revision and row["location"] == entry.expected_location:
        return "unchanged" if entry.desired_location == entry.expected_location else "pending"
    if (
        entry.desired_location != entry.expected_location
        and row["revision"] == entry.expected_revision + 1
        and row["location"] == entry.desired_location
    ):
        return "applied"
    raise LocationReviewError(
        f"Brand ID {entry.brand_id} изменён после проверки: "
        f"revision={row['revision']}, location={row['location']!r}"
    )


def apply_location_review(
    stores: StoreRepository,
    *,
    actor_id: int,
    apply: bool,
    path: Path = MANIFEST_PATH,
) -> dict[str, int | str]:
    """Dry-run or atomically apply reviewed manual locations."""

    if apply and (isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id < 1):
        raise LocationReviewError("Нужен положительный ID владельца для аудита")
    entries = load_manifest(path)
    context = stores.transaction() if apply else stores.connection()
    with context as connection:
        _validate_schema(connection)
        states = [(entry, _classify_entry(connection, entry)) for entry in entries]
        pending = [entry for entry, state in states if state == "pending"]
        if apply:
            for entry in pending:
                row = connection.execute(
                    "SELECT aliases_json FROM store_brands WHERE id=?", (entry.brand_id,)
                ).fetchone()
                stores.apply_change(
                    "edit_brand_names",
                    {
                        "brand_id": entry.brand_id,
                        "name": entry.expected_name,
                        "aliases": json.loads(row["aliases_json"]),
                        "location": entry.desired_location,
                    },
                    actor_id,
                    connection=connection,
                )
            for entry in pending:
                if _classify_entry(connection, entry) != "applied":
                    raise LocationReviewError(
                        f"Не удалось подтвердить запись brand ID {entry.brand_id}"
                    )
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick != "ok" or foreign_keys:
            raise LocationReviewError("Проверка целостности базы после manifest не пройдена")
        return {
            "mode": "apply" if apply else "dry-run",
            "reviewed": len(entries),
            "pending": len(pending) if not apply else 0,
            "changed": len(pending) if apply else 0,
            "already_current": len(entries) - len(pending),
        }


def main(argv: list[str] | None = None) -> None:
    """Run the reviewed address manifest against the configured stores DB."""

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("MCC_STORES_PATH", "var/stores.sqlite3")),
    )
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        actor_id = int(os.environ.get("BOT_OWNER_TELEGRAM_ID", "0"))
    except ValueError as exc:
        raise SystemExit("BOT_OWNER_TELEGRAM_ID must be a positive integer") from exc
    try:
        result = apply_location_review(
            StoreRepository(args.database),
            actor_id=actor_id,
            apply=args.apply,
            path=args.manifest,
        )
    except (LocationReviewError, StoreError, sqlite3.Error) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
