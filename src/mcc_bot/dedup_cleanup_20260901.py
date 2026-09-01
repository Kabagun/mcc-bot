"""Reviewed, exact-ID merchant cleanup for the 2026-09-01 store snapshot.

The command is deliberately dry-run by default.  ``--apply`` is required for
the transactional mutation, and every source/brand/partner row is addressed by
an ID or an exact reviewed source key.  Names are used only as drift checks;
this module never deletes by a generic name.
"""

# Reviewed merchant names intentionally contain mixed Cyrillic and Latin text.
# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .partner_rewards import PartnerOfferInput, PartnerRepository, PartnerTierInput
from .reviewed_store_policy import NETWORK_21VEK
from .stores import StoreError, StoreRepository

MANIFEST = "dedup-review-2026-09-01"
ACTOR_ENV = "BOT_OWNER_TELEGRAM_ID"

# These are source IDs, not merchant names.  They are stored as text because
# ``store_sources.store_id`` is text in the durable schema.
DELETED_TANNEI_SOURCE_IDS: tuple[str, ...] = (
    "1391019",
    "1509980",
    "1534752",
    "1520712",
    "1770238",
)

DELETED_PARTNER_SOURCE_KEYS: tuple[str, ...] = (
    "cactus:81415a90ecabeae920dd",
    "cactus:bb7c5f2e3492dcbaa835",
    "cactus:f4833a8bc49c708453a2",
    "cactus:5fe14f84a95e23367e17",
)

# The snapshot reviewed these exact source rows.  Their IDs are also useful
# for a cheap, explicit drift check before the destructive operation.
DELETED_SOURCE_EXPECTATIONS: dict[str, tuple[int, int, str, str, str]] = {
    # source_id: (brand_id, merchant_id, brand/merchant name, channel, identity)
    "1391019": (383, 383, "Услуги связи", "online", "tannei:online:1391019"),
    "1509980": (498, 498, "Услуги связи", "online", "tannei:online:1509980"),
    "1534752": (515, 515, "Услуги связи", "online", "tannei:online:1534752"),
    "1520712": (508, 508, "Мирум", "offline", "tannei:store:1520712"),
    "1770238": (714, 714, "Мирум", "offline", "tannei:store:1770238"),
}

DELETED_SOURCE_ROW_IDS: dict[str, int] = {
    "1391019": 539,
    "1509980": 743,
    "1534752": 778,
    "1520712": 757,
    "1770238": 1135,
}

# Every deleted Tannei source was reviewed together with this exact fact set.
# The cleanup must never broaden a delete merely because another row later
# points at the same merchant ID.
DELETED_FACT_EXPECTATIONS: dict[str, tuple[tuple[int, str], ...]] = {
    "1391019": ((408, "6012"),),
    "1509980": ((533, "4814"), (534, "6012"), (535, "4900")),
    "1534752": ((554, "6012"), (555, "4900")),
    "1520712": ((545, "5812"),),
    "1770238": ((772, "5411"),),
}

DELETED_SOURCE_ADDRESSES: dict[str, str | None] = {
    "1391019": None,
    "1509980": None,
    "1534752": None,
    "1520712": (
        "Гродненская область, Кореличский район, Мирский сельский Совет, Мир, "
        "Красноармейская улица, 1А"
    ),
    "1770238": (
        "Гродненская область, Кореличский район, Мирский сельский Совет, Мир, "
        "Красноармейская улица, 1А"
    ),
}

DELETED_CHECKPOINT_KEYS: tuple[str, ...] = tuple(
    key
    for source_id in DELETED_TANNEI_SOURCE_IDS
    for key in (f"done:{source_id}", f"error:{source_id}")
)

DELETED_PARTNER_SEED_IDS: dict[str, int] = {
    "brand:2b75223b7ce41437e08a": 30,
    "brand:69f8d947608c584ffa1e": 31,
    "brand:fa327563361a72956660": 33,
    "brand:c8be57618f0c48e39a91": 35,
}


@dataclass(frozen=True, slots=True)
class BrandMerge:
    """One reviewed source brand to surviving target brand relationship."""

    source_id: int
    target_id: int
    source_name: str
    target_name: str


# Brand-level merges intentionally preserve different-address channel
# merchants.  Same-address pairs are represented in MERCHANT_MERGES below and
# use the repository's fact/evidence/source moving semantics.
BRAND_MERGES: tuple[BrandMerge, ...] = (
    BrandMerge(70, 1025, "21vek.by", "21vek"),
    BrandMerge(640, 1025, "21vek.by", "21vek"),
    BrandMerge(1067, 202, "Unistore", "Unistore опт&розница"),
    BrandMerge(510, 4, "Mooon", "mooon.by"),
    BrandMerge(660, 4, "Mooon", "mooon.by"),
    BrandMerge(699, 4, "Mooon", "mooon.by"),
    BrandMerge(711, 1051, "Kakvapteke.by", "КАК В АПТЕКЕ"),
    BrandMerge(945, 821, "Многофункциональный комплекс «Мандарин»", "МФК «Мандарин»"),
    BrandMerge(790, 179, "Motorlend", "Motorland"),
    BrandMerge(595, 453, "Papa Doner", "Papa Doner"),
    BrandMerge(773, 613, "Salateira", "Salateira"),
    BrandMerge(798, 845, "Компьютерный Мир", "Компьютерный мир"),
    BrandMerge(589, 505, "Мода Макс", "МодаМакс"),
    BrandMerge(858, 181, "lamoda", "lamoda"),
    BrandMerge(892, 465, "life:)", "life:)"),
    BrandMerge(137, 61, "Elgato.by", "El Gato"),
    BrandMerge(729, 206, "oz.by", "OZ"),
    BrandMerge(646, 207, "7745.by", "7745 Большой магазин"),
    BrandMerge(246, 218, "conteshop.by", "Conte"),
    BrandMerge(789, 287, "officetonmarket.by", "Офистон Маркет"),
    BrandMerge(641, 22, "pass.rw.by", "Белорусская железная дорога"),
    BrandMerge(679, 78, "Приложение Дзякуй", "A-100"),
    BrandMerge(311, 239, "belpost.by", "Белпочта"),
)

# Exact pre-merge signatures from the reviewed snapshot.  A source row that
# is already archived into its declared target is accepted as the idempotent
# state; an active source/target must still match these aliases and revisions
# so a reused integer ID cannot pass the whitelist accidentally.
BRAND_PRE_STATES: dict[int, tuple[str, tuple[str, ...], int]] = {
    70: ("21vek.by", (), 1),
    1025: ("21vek", (), 1),
    640: ("21vek.by", (), 1),
    1067: ("Unistore", ("Uni store",), 1),
    202: ("Unistore опт&розница", ("Юнисторе",), 2),
    510: ("Mooon", (), 1),
    4: ("mooon.by", (), 1),
    660: ("Mooon", (), 1),
    699: ("Mooon", (), 1),
    711: ("Kakvapteke.by", (), 1),
    1051: ("КАК В АПТЕКЕ", (), 1),
    945: ("Многофункциональный комплекс «Мандарин»", ("Мандарин",), 1),
    821: ("МФК «Мандарин»", (), 1),
    790: ("Motorlend", (), 1),
    179: ("Motorland", (), 1),
    595: ("Papa Doner", (), 1),
    453: ("Papa Doner", (), 1),
    773: ("Salateira", (), 1),
    613: ("Salateira", (), 1),
    798: ("Компьютерный Мир", (), 1),
    845: ("Компьютерный мир", (), 1),
    589: ("Мода Макс", (), 1),
    505: ("МодаМакс", (), 1),
    858: ("lamoda", (), 1),
    181: ("lamoda", (), 1),
    892: ("life:)", (), 1),
    465: ("life:)", (), 1),
    137: ("Elgato.by", (), 1),
    61: ("El Gato", (), 1),
    729: ("oz.by", (), 1),
    206: ("OZ", (), 1),
    646: ("7745.by", (), 1),
    207: ("7745 Большой магазин", (), 1),
    246: ("conteshop.by", (), 1),
    218: ("Conte", (), 1),
    789: ("officetonmarket.by", (), 1),
    287: ("Офистон Маркет", (), 1),
    641: ("pass.rw.by", (), 1),
    22: ("Белорусская железная дорога", (), 1),
    679: ("Приложение Дзякуй", (), 1),
    78: ("A-100", (), 1),
    311: ("belpost.by", (), 1),
    239: ("Белпочта", (), 1),
}


MERCHANT_MERGES: tuple[tuple[int, int, str, str], ...] = (
    # source merchant, target merchant, source name, target name
    (677, 10, "Меридиан", "Меридиан"),
    (635, 583, "Кофе-автомат", "Кофе-автомат"),
)

MERCHANT_PRE_STATES: dict[int, tuple[str, str, str, int]] = {
    677: ("Меридиан", "offline", "tannei:store:1728829", 1),
    10: ("Меридиан", "offline", "tannei:store:1008029", 1),
    635: ("Кофе-автомат", "offline", "tannei:store:1664492", 1),
    583: ("Кофе-автомат", "offline", "tannei:store:1599168", 1),
}

# The reviewed same-address Coffee-automat decision is source-specific.  Keep
# both Tannei IDs attached to the surviving merchant; do not infer this merge
# from the display name alone.
COFFEE_AUTOMAT_SOURCE_IDS: tuple[str, str] = ("1599168", "1664492")
COFFEE_AUTOMAT_ADDRESS = "Брестская область, Кобрин, улица Николаева, 50"


# Final canonical spelling/aliases.  ``edit_brand_names`` is used only when a
# row differs, so a second apply creates no empty audit records.
CANONICAL_BRANDS: dict[int, tuple[str, tuple[str, ...]]] = {
    1025: ("21vek", ("21vek.by",)),
    202: ("Unistore", ("Unistore опт&розница", "Юнисторе", "Uni store")),
    4: ("Mooon", ("mooon.by",)),
    1051: ("КАК В АПТЕКЕ", ("Kakvapteke.by",)),
    821: ("МФК «Мандарин»", ("Мандарин",)),
    181: ("Lamoda", ("lamoda",)),
    845: ("Компьютерный мир", ("Компьютерный Мир",)),
    505: ("МодаМакс", ("Мода Макс",)),
    61: ("El Gato", ("Elgato.by",)),
    206: ("OZ", ("oz.by",)),
    207: ("7745 Большой магазин", ("7745.by",)),
    218: ("Conte", ("conteshop.by",)),
    287: ("Офистон Маркет", ("officetonmarket.by",)),
    22: ("Белорусская железная дорога", ("pass.rw.by",)),
    78: ("A-100", ("Приложение Дзякуй",)),
    239: ("Белпочта", ("belpost.by",)),
}

MANDARIN_MERCHANT_RENAME = (948, "Мандарин")
MANDARIN_MERCHANT_PRE_STATE = {
    "id": 948,
    "brand_id": 945,
    "name": "Многофункциональный комплекс «Мандарин»",
    "channel": "offline",
    "source_identity": None,
    "revision": 1,
}

VEK_FACTS_TO_ARCHIVE: tuple[int, ...] = (70, 360, 687, 688, 689, 1006)
VEK_FACT_EXPECTATIONS: dict[int, tuple[int, str]] = {
    70: (70, "5300"),
    360: (70, "5311"),
    687: (640, "5300"),
    688: (640, "5399"),
    689: (640, "5311"),
    1006: (1074, "5399"),
}
MERGED_FACT_EXPECTATIONS: dict[int, tuple[int, str]] = {
    729: (677, "5499"),
    682: (635, "5812"),
}
LEGACY_VEK_OFFER_ID = 115
LEGACY_VEK_SOURCE_KEY = "cashalot:21vek-by"
LEGACY_VEK_CONDITIONS = "Повышенный кэшбэк в партнёрской сети Cashalot."
LEGACY_VEK_SOURCE_URL = "https://cashalot.by/stores/store_select/21vek-by/"
CASHALOT_OFFER_ID = 207
CASHALOT_CARD_ID = "belgazprombank_cashalot"
CASHALOT_SOURCE_URL = "https://www.21vek.by/services/order.html"
CASHALOT_CONDITIONS = (
    "Действует при онлайн-оплате, оплате курьеру через терминал и в собственных "
    "пунктах выдачи независимо от MCC."
)
PARITET_SOURCE_KEY = "reviewed:20260901:21vek:paritet_combo"
PARITET_SOURCE_URL = CASHALOT_SOURCE_URL
MERIDIAN_SOURCE_IDS: tuple[str, str] = ("1728829", "1008029")
MERIDIAN_ADDRESS = "Брестская область, Кобрин, улица Дзержинского, 45А"


def _tombstone_reason() -> str:
    return f"{MANIFEST}: reviewed physical deletion"


class CleanupError(StoreError):
    """The reviewed snapshot no longer matches the database."""


def _placeholders(values: Iterable[Any]) -> str:
    values = tuple(values)
    if not values:
        raise ValueError("at least one SQL value is required")
    return ",".join("?" for _ in values)


def _required_tables(connection: sqlite3.Connection) -> None:
    required = {
        "store_brands",
        "store_brand_members",
        "store_merchants",
        "store_facts",
        "store_sources",
        "store_evidence",
        "store_tannei_import_guards",
        "store_tannei_snapshots",
        "store_tannei_tombstones",
        "partner_offers",
        "partner_offer_tiers",
        "partner_exclusions",
        "partner_seed_brands",
        "partner_seed_tombstones",
        "store_audit",
        "partner_audit",
    }
    found = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = sorted(required - found)
    if missing:
        raise CleanupError("Схема cleanup не инициализирована: " + ", ".join(missing))


def _brand_for_merchant(connection: sqlite3.Connection, merchant_id: int) -> int | None:
    row = connection.execute(
        "SELECT brand_id FROM store_brand_members WHERE merchant_id=?", (merchant_id,)
    ).fetchone()
    return int(row["brand_id"]) if row is not None else None


def _brand_state(connection: sqlite3.Connection, brand_id: int):
    return connection.execute("SELECT * FROM store_brands WHERE id=?", (brand_id,)).fetchone()


def _merchant_state(connection: sqlite3.Connection, merchant_id: int):
    return connection.execute("SELECT * FROM store_merchants WHERE id=?", (merchant_id,)).fetchone()


def _is_merged_into(row, target_id: int) -> bool:
    return row is not None and bool(row["archived"]) and row["merged_into"] == target_id


def _validate_brand_spec(connection: sqlite3.Connection, spec: BrandMerge) -> None:
    target = _brand_state(connection, spec.target_id)
    source = _brand_state(connection, spec.source_id)
    if target is None or target["archived"] or target["merged_into"] is not None:
        raise CleanupError(f"Целевая группа #{spec.target_id} изменилась после проверки")
    if source is None:
        raise CleanupError(f"Исходная группа #{spec.source_id} не найдена")
    if _is_merged_into(source, spec.target_id):
        return
    target_pre = BRAND_PRE_STATES.get(spec.target_id)
    source_pre = BRAND_PRE_STATES.get(spec.source_id)
    if target_pre is None or source_pre is None:
        raise CleanupError(
            f"Нет проверенной сигнатуры группы #{spec.source_id} или #{spec.target_id}"
        )
    try:
        target_aliases = tuple(json.loads(target["aliases_json"]))
        source_aliases = tuple(json.loads(source["aliases_json"]))
    except (TypeError, json.JSONDecodeError):
        raise CleanupError(f"Повреждены aliases у группы #{spec.target_id}") from None
    target_pre_ok = (
        target["name"] == target_pre[0]
        and target_aliases == target_pre[1]
        and int(target["revision"]) == target_pre[2]
    )
    canonical = CANONICAL_BRANDS.get(spec.target_id)
    target_post_ok = canonical is not None and (
        target["name"] == canonical[0] and target_aliases == canonical[1]
    )
    if not target_pre_ok and not target_post_ok:
        raise CleanupError(f"Целевая группа #{spec.target_id} изменилась после проверки")
    if (
        source["archived"]
        or source["merged_into"] is not None
        or source["name"] != spec.source_name
        or source_aliases != source_pre[1]
        or int(source["revision"]) != source_pre[2]
    ):
        raise CleanupError(f"Исходная группа #{spec.source_id} изменилась после проверки")


def _validate_merchant_spec(
    connection: sqlite3.Connection,
    source_id: int,
    target_id: int,
    source_name: str,
    target_name: str,
) -> None:
    target = _merchant_state(connection, target_id)
    source = _merchant_state(connection, source_id)
    if target is None or target["archived"] or target["name"] != target_name:
        raise CleanupError(f"Целевой вариант #{target_id} изменился после проверки")
    if source is None:
        raise CleanupError(f"Исходный вариант #{source_id} не найден")
    source_pre = MERCHANT_PRE_STATES.get(source_id)
    target_pre = MERCHANT_PRE_STATES.get(target_id)
    if source_pre is None or target_pre is None:
        raise CleanupError(f"Нет проверенной сигнатуры вариантов #{source_id} и #{target_id}")
    if source["archived"] and source["merged_into"] == target_id:
        expected_aliases = tuple(dict.fromkeys((target_pre[0], source_name)))
        try:
            target_aliases = tuple(json.loads(target["aliases_json"]))
        except (TypeError, json.JSONDecodeError):
            raise CleanupError(f"Повреждены aliases у варианта #{target_id}") from None
        if (
            target["channel"] != target_pre[1]
            or target["source_identity"] != target_pre[2]
            or target_aliases != expected_aliases
            or int(target["revision"]) < target_pre[3]
        ):
            raise CleanupError(f"Целевой вариант #{target_id} изменился после объединения")
        return
    if source["archived"] or source["merged_into"] is not None or source["name"] != source_name:
        raise CleanupError(f"Исходный вариант #{source_id} изменился после проверки")
    if source["channel"] != target["channel"]:
        raise CleanupError(f"Каналы вариантов #{source_id} и #{target_id} различаются")
    try:
        source_aliases = tuple(json.loads(source["aliases_json"]))
        target_aliases = tuple(json.loads(target["aliases_json"]))
    except (TypeError, json.JSONDecodeError):
        raise CleanupError(f"Повреждены aliases у вариантов #{source_id} и #{target_id}") from None
    if (
        source_aliases != ()
        or source["source_identity"] != source_pre[2]
        or int(source["revision"]) != source_pre[3]
        or target["name"] != target_pre[0]
        or target["channel"] != target_pre[1]
        or target_aliases != ()
        or target["source_identity"] != target_pre[2]
        or int(target["revision"]) != target_pre[3]
    ):
        raise CleanupError(f"Варианты #{source_id} и #{target_id} изменились после проверки")


def _validate_coffee_automat_sources(connection: sqlite3.Connection) -> None:
    """Require the two reviewed same-address Tannei source IDs.

    The merge itself is by merchant ID, but these exact source IDs are the
    evidence for the same-address decision.  After a successful merge both
    links point to merchant 583, so the check remains valid on an idempotent
    second apply.
    """

    placeholders = _placeholders(COFFEE_AUTOMAT_SOURCE_IDS)
    links = connection.execute(
        "SELECT * FROM store_sources "
        f"WHERE source='tannei' AND store_id IN ({placeholders}) ORDER BY store_id",
        COFFEE_AUTOMAT_SOURCE_IDS,
    ).fetchall()
    if len(links) != len(COFFEE_AUTOMAT_SOURCE_IDS):
        raise CleanupError("Источники Кофе-автомат изменились после проверки")
    for link in links:
        try:
            metadata = json.loads(link["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            raise CleanupError("Повреждены метаданные источника Кофе-автомат") from None
        expected_metadata = {
            "address": COFFEE_AUTOMAT_ADDRESS,
            "id": int(link["store_id"]),
            "is_online": False,
            "name": "Кофе-автомат",
            "network_id": None,
            "network_name": None,
        }
        if (
            int(link["merchant_id"]) not in {583, 635}
            or link["network_id"] is not None
            or metadata != expected_metadata
        ):
            raise CleanupError("Источники Кофе-автомат изменились после проверки")


def _validate_meridian_sources(connection: sqlite3.Connection) -> None:
    """Require the reviewed same-address Meridian source pair."""

    placeholders = _placeholders(MERIDIAN_SOURCE_IDS)
    links = connection.execute(
        "SELECT * FROM store_sources "
        f"WHERE source='tannei' AND store_id IN ({placeholders}) ORDER BY store_id",
        MERIDIAN_SOURCE_IDS,
    ).fetchall()
    if len(links) != len(MERIDIAN_SOURCE_IDS):
        raise CleanupError("Источники Меридиан изменились после проверки")
    for link in links:
        try:
            metadata = json.loads(link["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            raise CleanupError("Повреждены метаданные источника Меридиан") from None
        source_id = link["store_id"]
        expected_metadata = {
            "address": MERIDIAN_ADDRESS,
            "id": int(source_id),
            "is_online": False,
            "name": "Меридиан",
            "network_id": None,
            "network_name": None,
        }
        if (
            int(link["merchant_id"]) not in {677, 10}
            or link["network_id"] is not None
            or metadata != expected_metadata
        ):
            raise CleanupError("Источники Меридиан изменились после проверки")


def _source_residual_rows(
    connection: sqlite3.Connection, source_id: str, brand_id: int, merchant_id: int
) -> list[str]:
    """Return labels for any rows left after a tombstoned source delete."""

    residual: list[str] = []
    checks = (
        ("brand", "SELECT 1 FROM store_brands WHERE id=?", (brand_id,)),
        ("merchant", "SELECT 1 FROM store_merchants WHERE id=?", (merchant_id,)),
        (
            "member",
            "SELECT 1 FROM store_brand_members WHERE brand_id=? OR merchant_id=? LIMIT 1",
            (brand_id, merchant_id),
        ),
        ("source", "SELECT 1 FROM store_sources WHERE merchant_id=? LIMIT 1", (merchant_id,)),
        (
            "source_id",
            "SELECT 1 FROM store_sources WHERE source='tannei' AND store_id=? LIMIT 1",
            (source_id,),
        ),
        ("fact", "SELECT 1 FROM store_facts WHERE merchant_id=? LIMIT 1", (merchant_id,)),
        (
            "evidence",
            "SELECT 1 FROM store_evidence e JOIN store_facts f ON f.id=e.fact_id "
            "WHERE f.merchant_id=? LIMIT 1",
            (merchant_id,),
        ),
        (
            "guard",
            "SELECT 1 FROM store_tannei_import_guards WHERE store_source_id=? LIMIT 1",
            (DELETED_SOURCE_ROW_IDS[source_id],),
        ),
        ("snapshot", "SELECT 1 FROM store_tannei_snapshots WHERE brand_id=? LIMIT 1", (brand_id,)),
        ("partner_offer", "SELECT 1 FROM partner_offers WHERE brand_id=? LIMIT 1", (brand_id,)),
        (
            "partner_exclusion",
            "SELECT 1 FROM partner_exclusions WHERE brand_id=? LIMIT 1",
            (brand_id,),
        ),
        (
            "partner_seed",
            "SELECT 1 FROM partner_seed_brands WHERE brand_id=? LIMIT 1",
            (brand_id,),
        ),
        (
            "store_audit",
            "SELECT 1 FROM store_audit WHERE merchant_id=? OR brand_id=? LIMIT 1",
            (merchant_id, brand_id),
        ),
    )
    for label, query, values in checks:
        if connection.execute(query, values).fetchone():
            residual.append(label)
    checkpoint_rows = connection.execute(
        "SELECT 1 FROM store_import_checkpoints WHERE key IN (?,?) LIMIT 1",
        (f"done:{source_id}", f"error:{source_id}"),
    ).fetchone()
    if checkpoint_rows:
        residual.append("checkpoint")
    return residual


def _validate_deleted_sources(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    expected_reason = _tombstone_reason()
    for source_id, expected in DELETED_SOURCE_EXPECTATIONS.items():
        brand_id, merchant_id, expected_name, channel, identity = expected
        link = connection.execute(
            "SELECT * FROM store_sources WHERE source='tannei' AND store_id=?", (source_id,)
        ).fetchone()
        tombstone = connection.execute(
            "SELECT source_id,reason FROM store_tannei_tombstones WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if tombstone is not None and tombstone["reason"] != expected_reason:
            raise CleanupError(f"Блокировка источника Tannei #{source_id} имеет другую причину")
        if link is None:
            if tombstone is None:
                raise CleanupError(f"Проверенный источник Tannei #{source_id} не найден")
            # A tombstone is a no-op marker only after every reviewed child and
            # parent row is gone.  This catches interrupted/manual partial work.
            if _source_residual_rows(connection, source_id, brand_id, merchant_id):
                raise CleanupError(f"Источник Tannei #{source_id} удалён не полностью")
            result[source_id] = {
                "deleted": True,
                "brand_id": brand_id,
                "merchant_id": merchant_id,
                "checkpoint_keys": [],
            }
            continue
        if tombstone is not None:
            raise CleanupError(f"Источник Tannei #{source_id} всё ещё существует после блокировки")

        merchant = _merchant_state(connection, int(link["merchant_id"]))
        actual_brand = _brand_for_merchant(connection, int(link["merchant_id"]))
        try:
            metadata = json.loads(link["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            raise CleanupError(f"Метаданные источника Tannei #{source_id} повреждены") from None
        expected_address = DELETED_SOURCE_ADDRESSES[source_id]
        expected_online = channel == "online"
        expected_metadata = {
            "address": expected_address,
            "id": int(source_id),
            "is_online": expected_online,
            "name": expected_name,
            "network_id": None,
            "network_name": None,
        }
        if (
            merchant is None
            or actual_brand != brand_id
            or int(link["merchant_id"]) != merchant_id
            or int(link["id"]) != DELETED_SOURCE_ROW_IDS[source_id]
            or merchant["name"] != expected_name
            or merchant["channel"] != channel
            or merchant["source_identity"] != identity
            or link["source"] != "tannei"
            or link["store_id"] != source_id
            or link["network_id"] is not None
            or metadata != expected_metadata
        ):
            raise CleanupError(f"Источник Tannei #{source_id} изменился после проверки")

        # Every source merchant in the reviewed package has exactly one source
        # link and exactly the listed fact IDs/MCCs.  Evidence is intentionally
        # empty in the reviewed rows; any manual or unlisted evidence blocks.
        source_links = connection.execute(
            "SELECT id,source,store_id FROM store_sources WHERE merchant_id=? ORDER BY id",
            (merchant_id,),
        ).fetchall()
        if (
            len(source_links) != 1
            or int(source_links[0]["id"]) != DELETED_SOURCE_ROW_IDS[source_id]
        ):
            raise CleanupError(f"У источника Tannei #{source_id} появились другие source rows")
        facts = connection.execute(
            "SELECT id,mcc,merchant_id FROM store_facts WHERE merchant_id=? ORDER BY id",
            (merchant_id,),
        ).fetchall()
        expected_facts = set(DELETED_FACT_EXPECTATIONS[source_id])
        actual_facts = {(int(row["id"]), row["mcc"]) for row in facts}
        if actual_facts != expected_facts:
            raise CleanupError(f"Факты источника Tannei #{source_id} изменились после проверки")
        fact_ids = tuple(sorted(fact_id for fact_id, _mcc in expected_facts))
        evidence = connection.execute(
            "SELECT e.id,e.source,e.source_key FROM store_evidence e "
            f"WHERE e.fact_id IN ({_placeholders(fact_ids)}) ORDER BY e.id",
            fact_ids,
        ).fetchall()
        if evidence:
            raise CleanupError(f"У источника Tannei #{source_id} есть непроверенные evidence")

        members = connection.execute(
            "SELECT id,merchant_id FROM store_brand_members WHERE brand_id=? ORDER BY id",
            (brand_id,),
        ).fetchall()
        if len(members) != 1 or int(members[0]["merchant_id"]) != merchant_id:
            raise CleanupError(f"У источника Tannei #{source_id} появились другие варианты группы")
        guards = connection.execute(
            "SELECT g.id,g.store_source_id FROM store_tannei_import_guards g "
            "JOIN store_sources s ON s.id=g.store_source_id WHERE s.merchant_id=? ORDER BY g.id",
            (merchant_id,),
        ).fetchall()
        if len(guards) != 1 or int(guards[0]["store_source_id"]) != int(link["id"]):
            raise CleanupError(f"Защита импорта источника Tannei #{source_id} изменилась")

        # Deleted Tannei rows must not carry partner data or stale store audit
        # records.  These checks protect unrelated/manual rows before any
        # physical delete is attempted.
        for table in ("partner_offers", "partner_exclusions", "partner_seed_brands"):
            if connection.execute(
                f"SELECT 1 FROM {table} WHERE brand_id=? LIMIT 1", (brand_id,)
            ).fetchone():
                raise CleanupError(f"У источника Tannei #{source_id} есть партнёрские строки")
        if connection.execute(
            "SELECT 1 FROM store_audit WHERE merchant_id=? OR brand_id=? LIMIT 1",
            (merchant_id, brand_id),
        ).fetchone():
            raise CleanupError(f"У источника Tannei #{source_id} есть store_audit")
        snapshot_ids = tuple(
            int(row["id"])
            for row in connection.execute(
                "SELECT id FROM store_tannei_snapshots WHERE brand_id=? ORDER BY id", (brand_id,)
            ).fetchall()
        )
        if len(snapshot_ids) > 1:
            raise CleanupError(f"У источника Tannei #{source_id} появились snapshots")
        snapshot_rows = connection.execute(
            "SELECT id,snapshot_json FROM store_tannei_snapshots WHERE brand_id=? ORDER BY id",
            (brand_id,),
        ).fetchall()
        for snapshot in snapshot_rows:
            try:
                payload = json.loads(snapshot["snapshot_json"])
            except (TypeError, json.JSONDecodeError):
                raise CleanupError(f"Snapshot источника Tannei #{source_id} повреждён") from None
            stores = payload.get("stores") if isinstance(payload, dict) else None
            if not isinstance(stores, dict) or set(stores) != {source_id}:
                raise CleanupError(
                    f"Snapshot источника Tannei #{source_id} содержит другие source keys"
                )
            source_payload = stores[source_id]
            observations = (
                source_payload.get("observations") if isinstance(source_payload, dict) else None
            )
            expected_mccs = {mcc for _fact_id, mcc in DELETED_FACT_EXPECTATIONS[source_id]}
            observed_mccs = (
                {str(item.get("mcc")) for item in observations.values() if isinstance(item, dict)}
                if isinstance(observations, dict)
                else set()
            )
            if (
                not isinstance(source_payload, dict)
                or source_payload.get("channel") != channel
                or not isinstance(observations, dict)
                or not observed_mccs.issubset(expected_mccs)
                or not observed_mccs
            ):
                raise CleanupError(f"Snapshot источника Tannei #{source_id} изменился")
        checkpoint_keys = [
            row["key"]
            for row in connection.execute(
                "SELECT key FROM store_import_checkpoints WHERE key IN (?,?) ORDER BY key",
                (f"done:{source_id}", f"error:{source_id}"),
            ).fetchall()
        ]
        result[source_id] = {
            "deleted": False,
            "brand_id": brand_id,
            "merchant_id": merchant_id,
            "source_row_id": int(link["id"]),
            "fact_ids": list(fact_ids),
            "member_ids": [int(members[0]["id"])],
            "guard_ids": [int(guards[0]["id"])],
            "snapshot_ids": list(snapshot_ids),
            "checkpoint_keys": checkpoint_keys,
        }
    return result


def _validate_aqua(connection: sqlite3.Connection) -> dict[str, Any]:
    brand = _brand_state(connection, 944)
    merchant = _merchant_state(connection, 947)
    if brand is None and merchant is None:
        tombstones = connection.execute(
            "SELECT source_key,reason FROM partner_seed_tombstones "
            f"WHERE source_key IN ({_placeholders(DELETED_PARTNER_SOURCE_KEYS)})",
            DELETED_PARTNER_SOURCE_KEYS,
        ).fetchall()
        expected_reason = _tombstone_reason()
        if {row["source_key"] for row in tombstones} == set(DELETED_PARTNER_SOURCE_KEYS) and all(
            row["reason"] == expected_reason for row in tombstones
        ):
            return {"deleted": True, "offer_ids": [], "seed_ids": []}
        raise CleanupError("Проверенная группа Аква-Минск исчезла без полного набора tombstone")
    if brand is None or merchant is None or int(brand["id"]) != 944 or int(merchant["id"]) != 947:
        raise CleanupError("Группа Аква-Минск изменилась после проверки")
    member = connection.execute(
        "SELECT 1 FROM store_brand_members WHERE brand_id=944 AND merchant_id=947"
    ).fetchone()
    if member is None or brand["name"] != 'Отели "Аква-Минск"' or merchant["name"] != brand["name"]:
        raise CleanupError("Связь Аква-Минск изменилась после проверки")
    keys = _placeholders(DELETED_PARTNER_SOURCE_KEYS)
    offers = connection.execute(
        f"SELECT * FROM partner_offers WHERE source_key IN ({keys}) ORDER BY id",
        DELETED_PARTNER_SOURCE_KEYS,
    ).fetchall()
    if len(offers) != len(DELETED_PARTNER_SOURCE_KEYS) or any(
        int(row["brand_id"]) != 944
        or row["card_id"] != "cactus_mtbank"
        or row["channel"] != "any"
        or row["mode"] != "total"
        or row["reward_kind"] != "points"
        or row["archived"]
        for row in offers
    ):
        raise CleanupError("Партнёрская строка Аква-Минск привязана к другой группе")
    offer_ids = [int(row["id"]) for row in offers]
    if offer_ids:
        tiers = connection.execute(
            f"SELECT offer_id,value FROM partner_offer_tiers "
            f"WHERE offer_id IN ({_placeholders(offer_ids)}) ORDER BY offer_id,position,id",
            offer_ids,
        ).fetchall()
        if len(tiers) != len(offer_ids) or any(tier["value"] != "1.5" for tier in tiers):
            raise CleanupError("Тарифы Аква-Минск изменились после проверки")
    # A reviewed deletion must not silently remove a manually added row under
    # this brand.  The snapshot has no such rows; fail closed if one appears.
    unlisted = connection.execute(
        "SELECT id,source_key FROM partner_offers WHERE brand_id=944 "
        f"AND (source_key IS NULL OR source_key NOT IN ({keys}))",
        DELETED_PARTNER_SOURCE_KEYS,
    ).fetchall()
    if unlisted:
        raise CleanupError("У Аква-Минск появились непроверенные партнёрские строки")
    exclusions = connection.execute(
        "SELECT id,source_key FROM partner_exclusions WHERE brand_id=944"
    ).fetchall()
    if exclusions:
        raise CleanupError("У Аква-Минск появились непроверенные исключения")
    exclusion_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(partner_exclusions)")
    }
    if "offer_id" in exclusion_columns and offer_ids:
        linked_exclusions = connection.execute(
            f"SELECT id FROM partner_exclusions WHERE offer_id IN ({_placeholders(offer_ids)})",
            offer_ids,
        ).fetchall()
        if linked_exclusions:
            raise CleanupError("У Аква-Минск появились связанные исключения")
    seeds = connection.execute(
        "SELECT * FROM partner_seed_brands WHERE brand_id=944 ORDER BY id"
    ).fetchall()
    expected_seed_keys = set(DELETED_PARTNER_SEED_IDS)
    if {row["source_key"] for row in seeds} != expected_seed_keys or any(
        DELETED_PARTNER_SEED_IDS.get(row["source_key"]) != int(row["id"]) for row in seeds
    ):
        raise CleanupError("Привязки partner seed Аква-Минск изменились после проверки")
    return {
        "deleted": False,
        "offer_ids": [int(row["id"]) for row in offers],
        "seed_ids": [int(row["id"]) for row in seeds],
    }


def _validate_community_refs(
    connection: sqlite3.Connection, deleted_brands: set[int], deleted_merchants: set[int]
) -> None:
    def walk(value: Any):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key, item
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)

    sources: list[tuple[str, str, str]] = []
    if _table_exists(connection, "community_proposals"):
        rows = connection.execute(
            "SELECT id,payload FROM community_proposals "
            "WHERE status IN ('pending','claimed','clarification')"
        ).fetchall()
        sources.extend(("предложение", str(row["id"]), row["payload"]) for row in rows)
    if _table_exists(connection, "community_drafts"):
        rows = connection.execute("SELECT id,data FROM community_drafts").fetchall()
        sources.extend(("черновик", str(row["id"]), row["data"]) for row in rows)
    for kind, row_id, encoded in sources:
        try:
            payload = json.loads(encoded)
        except (TypeError, json.JSONDecodeError):
            raise CleanupError(f"Повреждены данные community {kind} #{row_id}") from None
        for key, value in walk(payload):
            try:
                numeric_value = int(value)
            except (TypeError, ValueError):
                continue
            if key == "brand_id" and numeric_value in deleted_brands:
                raise CleanupError(f"Community {kind} #{row_id} ссылается на удаляемую группу")
            if key == "merchant_id" and numeric_value in deleted_merchants:
                raise CleanupError(f"Community {kind} #{row_id} ссылается на удаляемый вариант")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _ensure_partner_columns(connection: sqlite3.Connection) -> None:
    """Apply the tiny partner migration needed by delete/update helpers.

    The reviewed snapshot predates ``partner_exclusions.offer_id`` while the
    current repository helper updates exclusions by that column.  Running the
    additive ``ALTER`` inside the cleanup transaction keeps the apply atomic;
    no separate bootstrap write is required.
    """

    columns = {row["name"] for row in connection.execute("PRAGMA table_info(partner_exclusions)")}
    if "offer_id" not in columns:
        connection.execute(
            "ALTER TABLE partner_exclusions ADD COLUMN offer_id INTEGER "
            "REFERENCES partner_offers(id)"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS partner_exclusions_offer "
        "ON partner_exclusions(offer_id,archived)"
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _filtered_vek_snapshot(
    connection: sqlite3.Connection, snapshot: sqlite3.Row
) -> tuple[dict[str, Any], list[tuple[sqlite3.Row, dict[str, Any]]]]:
    """Return a policy-pruned snapshot plus source payloads for guard hashes."""

    try:
        payload = json.loads(snapshot["snapshot_json"])
    except (TypeError, json.JSONDecodeError):
        raise CleanupError(f"Snapshot 21vek #{snapshot['id']} повреждён") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("stores"), dict):
        raise CleanupError(f"Snapshot 21vek #{snapshot['id']} имеет неверную структуру")
    stores = payload["stores"]
    guard_payloads: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    for source_id, source_payload in stores.items():
        link = connection.execute(
            "SELECT * FROM store_sources WHERE source='tannei' AND store_id=?", (source_id,)
        ).fetchone()
        if link is None:
            raise CleanupError(f"Snapshot 21vek ссылается на неизвестный источник #{source_id}")
        try:
            metadata = json.loads(link["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            raise CleanupError(f"Метаданные источника 21vek #{source_id} повреждены") from None
        try:
            network_id = int(metadata.get("network_id"))
        except (TypeError, ValueError):
            network_id = None
        if network_id != NETWORK_21VEK:
            continue
        if not isinstance(source_payload, dict) or not isinstance(
            source_payload.get("observations"), dict
        ):
            raise CleanupError(f"Snapshot 21vek для источника #{source_id} повреждён")
        filtered = dict(source_payload)
        if source_payload.get("channel") == "offline":
            filtered["observations"] = {}
        elif source_payload.get("channel") == "online":
            filtered["observations"] = {
                key: value
                for key, value in source_payload["observations"].items()
                if isinstance(value, dict) and str(value.get("mcc", "")).strip() == "5300"
            }
        else:
            raise CleanupError(f"Snapshot 21vek для источника #{source_id} имеет неверный канал")
        stores[source_id] = filtered
        guard_payloads.append((link, filtered))
    return payload, guard_payloads


def _snapshot_guard_changes(
    connection: sqlite3.Connection,
) -> tuple[list[int], list[int]]:
    snapshot_ids: list[int] = []
    guard_ids: list[int] = []
    rows = connection.execute(
        "SELECT * FROM store_tannei_snapshots WHERE brand_id IN (70,640,1025) ORDER BY id"
    ).fetchall()
    for snapshot in rows:
        payload, guard_payloads = _filtered_vek_snapshot(connection, snapshot)
        if _canonical_json(payload) != snapshot["snapshot_json"]:
            snapshot_ids.append(int(snapshot["id"]))
        for link, source_payload in guard_payloads:
            fingerprint = hashlib.sha256(
                f"{link['metadata_json']}\n{_canonical_json(source_payload)}".encode()
            ).hexdigest()
            guard = connection.execute(
                "SELECT id,fingerprint FROM store_tannei_import_guards WHERE store_source_id=?",
                (link["id"],),
            ).fetchone()
            if guard is None or guard["fingerprint"] != fingerprint:
                guard_ids.append(int(guard["id"])) if guard is not None else guard_ids.append(
                    int(link["id"])
                )
    return snapshot_ids, guard_ids


def _prune_vek_snapshots(connection: sqlite3.Connection) -> tuple[int, int]:
    """Apply the reviewed 21vek policy to stored snapshots and guard hashes."""

    snapshot_count = 0
    guard_count = 0
    rows = connection.execute(
        "SELECT * FROM store_tannei_snapshots WHERE brand_id IN (70,640,1025) ORDER BY id"
    ).fetchall()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for snapshot in rows:
        payload, guard_payloads = _filtered_vek_snapshot(connection, snapshot)
        encoded = _canonical_json(payload)
        if encoded != snapshot["snapshot_json"]:
            connection.execute(
                "UPDATE store_tannei_snapshots "
                "SET revision=revision+1,updated_at=?,snapshot_json=? "
                "WHERE id=?",
                (now, encoded, snapshot["id"]),
            )
            snapshot_count += 1
        for link, source_payload in guard_payloads:
            fingerprint = hashlib.sha256(
                f"{link['metadata_json']}\n{_canonical_json(source_payload)}".encode()
            ).hexdigest()
            guard = connection.execute(
                "SELECT * FROM store_tannei_import_guards WHERE store_source_id=?",
                (link["id"],),
            ).fetchone()
            if guard is None:
                merchant = _merchant_state(connection, int(link["merchant_id"]))
                if merchant is None or merchant["source_identity"] is None:
                    raise CleanupError(f"Источник 21vek #{link['store_id']} не имеет identity")
                connection.execute(
                    "INSERT INTO store_tannei_import_guards"
                    "(store_source_id,source_identity,revision,fingerprint) VALUES(?,?,1,?)",
                    (link["id"], merchant["source_identity"], fingerprint),
                )
                guard_count += 1
            elif guard["fingerprint"] != fingerprint:
                connection.execute(
                    "UPDATE store_tannei_import_guards SET revision=revision+1,"
                    "fingerprint=?,last_legacy_audit_id=NULL WHERE id=?",
                    (fingerprint, guard["id"]),
                )
                guard_count += 1
    return snapshot_count, guard_count


def _fact_rows(connection: sqlite3.Connection, fact_ids: Iterable[int]) -> list[sqlite3.Row]:
    ids = tuple(fact_ids)
    if not ids:
        return []
    return connection.execute(
        f"SELECT * FROM store_facts WHERE id IN ({_placeholders(ids)}) ORDER BY id", ids
    ).fetchall()


def _validate_fact_expectations(
    connection: sqlite3.Connection, expectations: dict[int, tuple[int, str]], label: str
) -> None:
    for fact_id, (merchant_id, mcc) in expectations.items():
        row = connection.execute(
            "SELECT id,merchant_id,mcc FROM store_facts WHERE id=?", (fact_id,)
        ).fetchone()
        if row is None or int(row["merchant_id"]) != merchant_id or row["mcc"] != mcc:
            raise CleanupError(f"Факт {label} #{fact_id} изменился после проверки")


def _validate_vek_facts(connection: sqlite3.Connection) -> None:
    _validate_fact_expectations(connection, VEK_FACT_EXPECTATIONS, "21vek")
    _validate_fact_expectations(connection, MERGED_FACT_EXPECTATIONS, "same-address")


def _validate_legacy_vek_offer(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT * FROM partner_offers WHERE id=?", (LEGACY_VEK_OFFER_ID,)
    ).fetchone()
    if row is None:
        raise CleanupError("Старое предложение Cashalot #115 не найдено")
    if (
        row["source_key"] != LEGACY_VEK_SOURCE_KEY
        or row["brand_id"] not in {640, 1025}
        or row["card_id"] != CASHALOT_CARD_ID
        or row["channel"] != "online"
        or row["mode"] != "total"
        or row["reward_kind"] != "cash"
        or row["conditions"] != LEGACY_VEK_CONDITIONS
        or row["source_url"] != LEGACY_VEK_SOURCE_URL
    ):
        raise CleanupError("Старое предложение Cashalot #115 изменилось после проверки")
    tiers = connection.execute(
        "SELECT value,min_purchase,max_purchase,per_transaction_cap,starts_on,ends_on "
        "FROM partner_offer_tiers WHERE offer_id=? ORDER BY position,id",
        (LEGACY_VEK_OFFER_ID,),
    ).fetchall()
    if (
        len(tiers) != 1
        or tiers[0]["value"] != "1.5"
        or any(
            tiers[0][field] is not None
            for field in (
                "min_purchase",
                "max_purchase",
                "per_transaction_cap",
                "starts_on",
                "ends_on",
            )
        )
    ):
        raise CleanupError("Тариф старого предложения Cashalot #115 изменился")


def _validate_mandarin_merchant(connection: sqlite3.Connection) -> None:
    row = _merchant_state(connection, MANDARIN_MERCHANT_PRE_STATE["id"])
    member = connection.execute(
        "SELECT brand_id FROM store_brand_members WHERE merchant_id=?",
        (MANDARIN_MERCHANT_PRE_STATE["id"],),
    ).fetchone()
    if row is None or member is None:
        raise CleanupError("Вариант Мандарин #948 не найден")
    aliases = tuple(json.loads(row["aliases_json"]))
    pre = MANDARIN_MERCHANT_PRE_STATE
    if (
        not row["archived"]
        and int(member["brand_id"]) == pre["brand_id"]
        and row["name"] == pre["name"]
        and row["channel"] == pre["channel"]
        and row["source_identity"] == pre["source_identity"]
        and int(row["revision"]) == pre["revision"]
        and aliases == ("Мандарин",)
    ):
        return
    if (
        not row["archived"]
        and int(member["brand_id"]) == 821
        and row["name"] == "Мандарин"
        and aliases == ("Мандарин",)
        and row["source_identity"] is None
    ):
        return
    raise CleanupError("Вариант Мандарин #948 изменился после проверки")


def _build_plan(connection: sqlite3.Connection) -> dict[str, Any]:
    _required_tables(connection)
    _validate_vek_facts(connection)
    _validate_legacy_vek_offer(connection)
    _validate_mandarin_merchant(connection)
    deleted_sources = _validate_deleted_sources(connection)
    aqua = _validate_aqua(connection)
    deleted_brands = {value["brand_id"] for value in deleted_sources.values()}
    deleted_merchants = {value["merchant_id"] for value in deleted_sources.values()}
    if not aqua["deleted"]:
        deleted_brands.add(944)
        deleted_merchants.add(947)
    _validate_community_refs(connection, deleted_brands, deleted_merchants)
    _validate_coffee_automat_sources(connection)
    _validate_meridian_sources(connection)
    for spec in BRAND_MERGES:
        _validate_brand_spec(connection, spec)
    for source_id, target_id, source_name, target_name in MERCHANT_MERGES:
        _validate_merchant_spec(connection, source_id, target_id, source_name, target_name)
    for brand_id, (_name, _aliases) in CANONICAL_BRANDS.items():
        row = _brand_state(connection, brand_id)
        if row is None or row["archived"] or row["merged_into"] is not None:
            raise CleanupError(f"Каноническая группа #{brand_id} недоступна")

    active_states = {
        source_id: state for source_id, state in deleted_sources.items() if not state["deleted"]
    }
    would_delete: dict[str, list[Any]] = {
        "source_ids": list(active_states),
        "source_row_ids": [state["source_row_id"] for state in active_states.values()],
        "brands": sorted({state["brand_id"] for state in active_states.values()}),
        "merchants": sorted({state["merchant_id"] for state in active_states.values()}),
        "facts": [],
        "evidence": [],
        "guards": [],
        "snapshots": [],
        "members": [],
        "checkpoints": [],
        "offers": [],
        "offer_tiers": [],
        "partner_seeds": [],
        "store_audits": [],
        "partner_audits": [],
    }
    # Only rows that still exist are reported as would-delete.  This makes a
    # second plan an explicit no-op while retaining the identity evidence.
    fact_ids = sorted(
        {fact_id for state in active_states.values() for fact_id in state["fact_ids"]}
    )
    would_delete["facts"] = fact_ids
    if fact_ids:
        ev = connection.execute(
            "SELECT id FROM store_evidence "
            f"WHERE fact_id IN ({_placeholders(fact_ids)}) ORDER BY id",
            fact_ids,
        ).fetchall()
        would_delete["evidence"] = [int(row["id"]) for row in ev]
    would_delete["guards"] = sorted(
        guard_id for state in active_states.values() for guard_id in state["guard_ids"]
    )
    would_delete["snapshots"] = sorted(
        snapshot_id for state in active_states.values() for snapshot_id in state["snapshot_ids"]
    )
    would_delete["members"] = sorted(
        member_id for state in active_states.values() for member_id in state["member_ids"]
    )
    would_delete["checkpoints"] = sorted(
        key for state in active_states.values() for key in state["checkpoint_keys"]
    )
    if not aqua["deleted"]:
        would_delete["offers"] = aqua["offer_ids"]
        if aqua["offer_ids"]:
            tiers = connection.execute(
                "SELECT id FROM partner_offer_tiers "
                f"WHERE offer_id IN ({_placeholders(aqua['offer_ids'])}) ORDER BY id",
                tuple(aqua["offer_ids"]),
            ).fetchall()
            would_delete["offer_tiers"] = [int(row["id"]) for row in tiers]
        would_delete["partner_seeds"] = aqua["seed_ids"]
        would_delete["store_audits"] = [
            int(row["id"])
            for row in connection.execute("SELECT id FROM store_audit WHERE id=1638").fetchall()
        ]
        if aqua["offer_ids"]:
            would_delete["partner_audits"] = [
                int(row["id"])
                for row in connection.execute(
                    "SELECT id FROM partner_audit "
                    f"WHERE entity_id IN ({_placeholders(aqua['offer_ids'])}) ORDER BY id",
                    tuple(aqua["offer_ids"]),
                ).fetchall()
            ]

    would_merge: list[dict[str, Any]] = []
    already_merged: list[dict[str, Any]] = []
    for spec in BRAND_MERGES:
        source = _brand_state(connection, spec.source_id)
        if source is not None and _is_merged_into(source, spec.target_id):
            already_merged.append(
                {"kind": "brand", "source_id": spec.source_id, "target_id": spec.target_id}
            )
        else:
            would_merge.append(
                {"kind": "brand", "source_id": spec.source_id, "target_id": spec.target_id}
            )
    for source_id, target_id, _source_name, _target_name in MERCHANT_MERGES:
        source = _merchant_state(connection, source_id)
        if source is not None and source["archived"] and source["merged_into"] == target_id:
            already_merged.append(
                {"kind": "merchant", "source_id": source_id, "target_id": target_id}
            )
        else:
            would_merge.append({"kind": "merchant", "source_id": source_id, "target_id": target_id})

    would_archive: dict[str, list[int]] = {"facts": [], "offers": []}
    for fact_id in VEK_FACTS_TO_ARCHIVE:
        row = connection.execute(
            "SELECT archived FROM store_facts WHERE id=?", (fact_id,)
        ).fetchone()
        if row is not None and not row["archived"]:
            would_archive["facts"].append(fact_id)
    row = connection.execute(
        "SELECT archived FROM partner_offers WHERE id=?", (LEGACY_VEK_OFFER_ID,)
    ).fetchone()
    if row is not None and not row["archived"]:
        would_archive["offers"].append(LEGACY_VEK_OFFER_ID)
    # A same-address merchant merge archives duplicate source facts through the
    # repository API; expose those identities in the dry-run manifest.
    for fact_id in (729, 682):
        row = connection.execute(
            "SELECT archived FROM store_facts WHERE id=?", (fact_id,)
        ).fetchone()
        if row is not None and not row["archived"]:
            would_archive["facts"].append(fact_id)

    would_update: list[dict[str, Any]] = []
    for brand_id, (name, aliases) in CANONICAL_BRANDS.items():
        row = _brand_state(connection, brand_id)
        if row is None:
            raise CleanupError(f"Каноническая группа #{brand_id} не найдена")
        try:
            current_aliases = tuple(json.loads(row["aliases_json"]))
        except (TypeError, json.JSONDecodeError):
            raise CleanupError(f"Повреждены aliases у группы #{brand_id}") from None
        if row["name"] != name or current_aliases != aliases:
            would_update.append(
                {"kind": "brand", "id": brand_id, "name": name, "aliases": list(aliases)}
            )
    merchant = _merchant_state(connection, MANDARIN_MERCHANT_RENAME[0])
    if (
        merchant is not None
        and not merchant["archived"]
        and merchant["name"] != MANDARIN_MERCHANT_RENAME[1]
    ):
        would_update.append(
            {
                "kind": "merchant",
                "id": MANDARIN_MERCHANT_RENAME[0],
                "name": MANDARIN_MERCHANT_RENAME[1],
            }
        )

    paritet = connection.execute(
        "SELECT * FROM partner_offers WHERE source_key=?", (PARITET_SOURCE_KEY,)
    ).fetchone()
    would_add: dict[str, list[Any]] = {
        "tannei_tombstones": [
            source_id
            for source_id in DELETED_TANNEI_SOURCE_IDS
            if connection.execute(
                "SELECT 1 FROM store_tannei_tombstones WHERE source_id=?", (source_id,)
            ).fetchone()
            is None
        ],
        "partner_tombstones": [
            source_key
            for source_key in DELETED_PARTNER_SOURCE_KEYS
            if connection.execute(
                "SELECT 1 FROM partner_seed_tombstones WHERE source_key=?", (source_key,)
            ).fetchone()
            is None
        ],
        "offers": []
        if paritet is not None
        else [
            {
                "source_key": PARITET_SOURCE_KEY,
                "brand_id": 1025,
                "card_id": "paritet_combo",
                "channel": "offline",
                "mode": "total",
                "reward_kind": "cash",
                "tier": "2.4",
                "conditions": "ПВЗ или курьеру",
            }
        ],
    }
    counts = {
        "would_delete": sum(len(values) for values in would_delete.values()),
        "would_merge": len(would_merge),
        "would_archive": sum(len(values) for values in would_archive.values()),
        "would_update": len(would_update),
        "would_add": sum(len(values) for values in would_add.values()),
        "already_merged": len(already_merged),
    }
    return {
        "manifest": MANIFEST,
        "would_delete": would_delete,
        "would_merge": would_merge,
        "already_merged": already_merged,
        "would_archive": would_archive,
        "would_update": would_update,
        "would_add": would_add,
        "counts": counts,
    }


def build_cleanup_plan(repository: StoreRepository) -> dict[str, Any]:
    """Return an exact, non-mutating cleanup plan."""

    with repository.connection() as connection:
        return _build_plan(connection)


def _archive_fact(
    repository: StoreRepository, connection: sqlite3.Connection, fact_id: int, actor_id: int
) -> bool:
    row = connection.execute(
        "SELECT merchant_id,mcc,archived FROM store_facts WHERE id=?", (fact_id,)
    ).fetchone()
    if row is None or row["archived"]:
        return False
    result = repository.apply_change(
        "archive_mcc",
        {"merchant_id": int(row["merchant_id"]), "mcc": row["mcc"]},
        actor_id,
        connection=connection,
    )
    return bool(result.audit_id)


def _archive_offer(
    partners: PartnerRepository, connection: sqlite3.Connection, offer_id: int, actor_id: int
) -> bool:
    row = connection.execute(
        "SELECT archived FROM partner_offers WHERE id=?", (offer_id,)
    ).fetchone()
    return bool(
        row is not None
        and not row["archived"]
        and partners.delete_offer(offer_id, actor_id=actor_id, connection=connection)
    )


def _set_brand(
    repository: StoreRepository,
    connection: sqlite3.Connection,
    brand_id: int,
    name: str,
    aliases: tuple[str, ...],
    actor_id: int,
) -> bool:
    row = _brand_state(connection, brand_id)
    if row is None:
        raise CleanupError(f"Каноническая группа #{brand_id} не найдена")
    current = tuple(json.loads(row["aliases_json"]))
    if row["name"] == name and current == aliases:
        return False
    repository.apply_change(
        "edit_brand_names",
        {"brand_id": brand_id, "name": name, "aliases": aliases},
        actor_id,
        connection=connection,
    )
    return True


def _set_merchant_name(
    repository: StoreRepository,
    connection: sqlite3.Connection,
    merchant_id: int,
    name: str,
    actor_id: int,
) -> bool:
    row = _merchant_state(connection, merchant_id)
    if row is None or row["archived"]:
        return False
    if row["name"] == name:
        return False
    repository.apply_change(
        "rename_merchant",
        {"merchant_id": merchant_id, "name": name},
        actor_id,
        connection=connection,
    )
    return True


def _ensure_paritet(
    partners: PartnerRepository, connection: sqlite3.Connection, actor_id: int
) -> bool:
    row = connection.execute(
        "SELECT * FROM partner_offers WHERE source_key=?", (PARITET_SOURCE_KEY,)
    ).fetchone()
    if row is not None:
        if (
            row["brand_id"] != 1025
            or row["card_id"] != "paritet_combo"
            or row["channel"] != "offline"
            or row["mode"] != "total"
            or row["reward_kind"] != "cash"
        ):
            raise CleanupError("Существующее предложение Paritet изменилось после проверки")
        if row["archived"]:
            raise CleanupError(
                "Предложение Paritet было архивировано отдельно; требуется ручное решение"
            )
        tier = connection.execute(
            "SELECT value,min_purchase,max_purchase,per_transaction_cap "
            "FROM partner_offer_tiers WHERE offer_id=? ORDER BY position,id",
            (row["id"],),
        ).fetchall()
        if len(tier) != 1 or tier[0]["value"] != "2.4":
            raise CleanupError("Тариф Paritet изменился после проверки")
        return False
    active = connection.execute(
        """SELECT id FROM partner_offers WHERE brand_id=1025 AND card_id='paritet_combo'
           AND channel='offline' AND mode='total' AND reward_kind='cash' AND archived=0"""
    ).fetchall()
    if active:
        raise CleanupError("У 21vek уже есть непомеченное предложение Paritet; дубликат запрещён")
    partners.create_offer(
        PartnerOfferInput(
            brand_id=1025,
            card_id="paritet_combo",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(value=Decimal("2.4")),),
            conditions="ПВЗ или курьеру",
            source_url="",
        ),
        actor_id=actor_id,
        connection=connection,
        source_key=PARITET_SOURCE_KEY,
    )
    return True


def _delete_aqua(connection: sqlite3.Connection, aqua: dict[str, Any]) -> dict[str, int]:
    if aqua["deleted"]:
        return {}
    offers = tuple(aqua["offer_ids"])
    tier_rows = 0
    if offers:
        tier_rows = connection.execute(
            f"SELECT count(*) FROM partner_offer_tiers WHERE offer_id IN ({_placeholders(offers)})",
            offers,
        ).fetchone()[0]
        connection.execute(
            f"DELETE FROM partner_offer_tiers WHERE offer_id IN ({_placeholders(offers)})", offers
        )
        connection.execute(
            f"DELETE FROM partner_offers WHERE id IN ({_placeholders(offers)})", offers
        )
    if aqua["seed_ids"]:
        connection.execute(
            f"DELETE FROM partner_seed_brands WHERE id IN ({_placeholders(aqua['seed_ids'])})",
            tuple(aqua["seed_ids"]),
        )
    # These are immutable rows from the reviewed manual insertion and are the
    # only audits explicitly covered by the deletion decision.
    connection.execute("DELETE FROM store_audit WHERE id=1638")
    if offers:
        connection.execute(
            f"DELETE FROM partner_audit WHERE entity_id IN ({_placeholders(offers)})", offers
        )
    connection.execute("DELETE FROM store_brand_members WHERE brand_id=944 AND merchant_id=947")
    connection.execute("DELETE FROM store_merchants WHERE id=947")
    connection.execute("DELETE FROM store_brands WHERE id=944")
    return {
        "brands": 1,
        "merchants": 1,
        "offers": len(offers),
        "offer_tiers": tier_rows,
        "seeds": len(aqua["seed_ids"]),
        "audits": 1 + len(offers),
    }


def _delete_tannei_sources(
    connection: sqlite3.Connection, states: dict[str, dict[str, Any]]
) -> dict[str, int]:
    active = [(source_id, state) for source_id, state in states.items() if not state["deleted"]]
    if not active:
        return {}
    counts = {
        "brands": 0,
        "merchants": 0,
        "facts": 0,
        "evidence": 0,
        "sources": 0,
        "guards": 0,
        "snapshots": 0,
        "members": 0,
        "checkpoints": 0,
    }
    # All IDs were captured by the fail-closed preflight above.  Delete those
    # exact children only; never broaden by merchant/brand membership here.
    for _source_id, state in active:
        fact_ids = tuple(state["fact_ids"])
        if fact_ids:
            counts["evidence"] += connection.execute(
                f"SELECT count(*) FROM store_evidence WHERE fact_id IN ({_placeholders(fact_ids)})",
                fact_ids,
            ).fetchone()[0]
            connection.execute(
                f"DELETE FROM store_evidence WHERE fact_id IN ({_placeholders(fact_ids)})",
                fact_ids,
            )
        guard_ids = tuple(state["guard_ids"])
        if guard_ids:
            counts["guards"] += connection.execute(
                "SELECT count(*) FROM store_tannei_import_guards "
                f"WHERE id IN ({_placeholders(guard_ids)})",
                guard_ids,
            ).fetchone()[0]
            connection.execute(
                f"DELETE FROM store_tannei_import_guards WHERE id IN ({_placeholders(guard_ids)})",
                guard_ids,
            )
        snapshot_ids = tuple(state["snapshot_ids"])
        if snapshot_ids:
            counts["snapshots"] += connection.execute(
                "SELECT count(*) FROM store_tannei_snapshots "
                f"WHERE id IN ({_placeholders(snapshot_ids)})",
                snapshot_ids,
            ).fetchone()[0]
            connection.execute(
                f"DELETE FROM store_tannei_snapshots WHERE id IN ({_placeholders(snapshot_ids)})",
                snapshot_ids,
            )
        source_row_id = int(state["source_row_id"])
        counts["sources"] += connection.execute(
            "SELECT count(*) FROM store_sources WHERE id=?", (source_row_id,)
        ).fetchone()[0]
        connection.execute("DELETE FROM store_sources WHERE id=?", (source_row_id,))
        if fact_ids:
            counts["facts"] += connection.execute(
                f"SELECT count(*) FROM store_facts WHERE id IN ({_placeholders(fact_ids)})",
                fact_ids,
            ).fetchone()[0]
            connection.execute(
                f"DELETE FROM store_facts WHERE id IN ({_placeholders(fact_ids)})", fact_ids
            )
        member_ids = tuple(state["member_ids"])
        if member_ids:
            counts["members"] += connection.execute(
                "SELECT count(*) FROM store_brand_members "
                f"WHERE id IN ({_placeholders(member_ids)})",
                member_ids,
            ).fetchone()[0]
            connection.execute(
                f"DELETE FROM store_brand_members WHERE id IN ({_placeholders(member_ids)})",
                member_ids,
            )
        checkpoint_keys = tuple(state["checkpoint_keys"])
        if checkpoint_keys:
            counts["checkpoints"] += connection.execute(
                "SELECT count(*) FROM store_import_checkpoints "
                f"WHERE key IN ({_placeholders(checkpoint_keys)})",
                checkpoint_keys,
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM store_import_checkpoints "
                f"WHERE key IN ({_placeholders(checkpoint_keys)})",
                checkpoint_keys,
            )
        connection.execute("DELETE FROM store_merchants WHERE id=?", (state["merchant_id"],))
        connection.execute("DELETE FROM store_brands WHERE id=?", (state["brand_id"],))
        counts["merchants"] += 1
        counts["brands"] += 1
    return counts


def _preflight(connection: sqlite3.Connection) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    _required_tables(connection)
    _validate_vek_facts(connection)
    _validate_legacy_vek_offer(connection)
    states = _validate_deleted_sources(connection)
    aqua = _validate_aqua(connection)
    deleted_brands = {value["brand_id"] for value in states.values()}
    deleted_merchants = {value["merchant_id"] for value in states.values()}
    if not aqua["deleted"]:
        deleted_brands.add(944)
        deleted_merchants.add(947)
    _validate_community_refs(connection, deleted_brands, deleted_merchants)
    _validate_coffee_automat_sources(connection)
    _validate_meridian_sources(connection)
    for spec in BRAND_MERGES:
        _validate_brand_spec(connection, spec)
    for source_id, target_id, source_name, target_name in MERCHANT_MERGES:
        _validate_merchant_spec(connection, source_id, target_id, source_name, target_name)
    for brand_id, (_name, _aliases) in CANONICAL_BRANDS.items():
        row = _brand_state(connection, brand_id)
        if row is None or row["archived"] or row["merged_into"] is not None:
            raise CleanupError(f"Каноническая группа #{brand_id} недоступна")
    return states, aqua


def apply_cleanup(
    repository: StoreRepository,
    *,
    actor_id: int,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan or apply the reviewed cleanup.

    ``dry_run=True`` (the default) performs only reads.  The apply path holds
    one ``BEGIN IMMEDIATE`` transaction, validates the reviewed IDs again, and
    rolls back on any drift or integrity failure.
    """

    if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id < 1:
        raise CleanupError("Нужен положительный ID владельца для аудита")
    if dry_run:
        plan = build_cleanup_plan(repository)
        return {"mode": "dry-run", **plan}

    # Partner tables are part of the reviewed snapshot and are initialized by
    # the normal application bootstrap.  Refuse to mutate a partially created
    # database rather than creating schema outside the one apply transaction.
    with repository.transaction() as connection:
        states, aqua = _preflight(connection)
        _ensure_partner_columns(connection)
        inserted_tannei_tombstones = sum(
            repository.tombstone_tannei_source(
                source_id,
                _tombstone_reason(),
                connection=connection,
            )
            for source_id in DELETED_TANNEI_SOURCE_IDS
        )
        inserted_partner_tombstones = sum(
            repository.tombstone_partner_seed(
                source_key,
                _tombstone_reason(),
                connection=connection,
            )
            for source_key in DELETED_PARTNER_SOURCE_KEYS
        )

        partners = PartnerRepository(repository)
        stats: dict[str, Any] = {
            "tombstones": {
                "tannei": inserted_tannei_tombstones,
                "partner": inserted_partner_tombstones,
            },
            "merged": {"brand": 0, "merchant": 0},
            "archived": {"facts": 0, "offers": 0},
            "updated": {"brands": 0, "merchants": 0},
            "added": {"offers": 0},
            "deleted": {},
        }
        # Resolve partner overlap before merging B640 into 21vek.
        if _archive_offer(partners, connection, LEGACY_VEK_OFFER_ID, actor_id):
            stats["archived"]["offers"] += 1

        # Same-address merchant merges must run before any standalone brand
        # merge for their source brand; the repository moves facts/evidence and
        # then archives the source brand as one audited operation.
        for source_id, target_id, _source_name, _target_name in MERCHANT_MERGES:
            source = _merchant_state(connection, source_id)
            if source is not None and not source["archived"]:
                repository.apply_change(
                    "merge_merchant",
                    {"merchant_id": source_id, "target_id": target_id},
                    actor_id,
                    connection=connection,
                )
                stats["merged"]["merchant"] += 1

        for spec in BRAND_MERGES:
            source = _brand_state(connection, spec.source_id)
            if source is not None and not source["archived"]:
                repository.apply_change(
                    "merge_brand",
                    {"brand_id": spec.source_id, "target_id": spec.target_id},
                    actor_id,
                    connection=connection,
                )
                stats["merged"]["brand"] += 1

        for brand_id, (name, aliases) in CANONICAL_BRANDS.items():
            if _set_brand(repository, connection, brand_id, name, aliases, actor_id):
                stats["updated"]["brands"] += 1
        if _set_merchant_name(
            repository,
            connection,
            MANDARIN_MERCHANT_RENAME[0],
            MANDARIN_MERCHANT_RENAME[1],
            actor_id,
        ):
            stats["updated"]["merchants"] += 1

        for fact_id in VEK_FACTS_TO_ARCHIVE:
            if _archive_fact(repository, connection, fact_id, actor_id):
                stats["archived"]["facts"] += 1
        # The same-address merge archives these duplicate source facts.  They
        # are counted by the merge operation, not archived a second time.
        for deletion_stats in (
            _delete_aqua(connection, aqua),
            _delete_tannei_sources(connection, states),
        ):
            for key, value in deletion_stats.items():
                stats["deleted"][key] = stats["deleted"].get(key, 0) + value

        if _ensure_paritet(partners, connection, actor_id):
            stats["added"]["offers"] += 1

        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick != "ok" or violations:
            raise CleanupError("Проверка SQLite не пройдена после cleanup")

    plan = build_cleanup_plan(repository)
    return {"mode": "apply", "dry_run": False, "applied": stats, "post_plan": plan}


def main(argv: list[str] | None = None) -> None:
    """Print a dry-run manifest, or apply it with explicit ``--apply``."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("MCC_STORES_PATH", "var/stores.sqlite3")),
    )
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed transaction")
    parser.add_argument("--actor-id", type=int, help="Owner ID used for audited updates")
    args = parser.parse_args(argv)
    if not args.database.is_file():
        raise SystemExit(f"Database not found: {args.database}")
    raw_actor = args.actor_id
    if raw_actor is None:
        raw_env = os.environ.get(ACTOR_ENV, "")
        try:
            raw_actor = int(raw_env)
        except ValueError:
            raw_actor = 1 if not args.apply else 0
    repository = StoreRepository(args.database)
    result = apply_cleanup(repository, actor_id=raw_actor, dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = [
    "BRAND_MERGES",
    "CANONICAL_BRANDS",
    "COFFEE_AUTOMAT_SOURCE_IDS",
    "DELETED_PARTNER_SOURCE_KEYS",
    "DELETED_TANNEI_SOURCE_IDS",
    "MERCHANT_MERGES",
    "PARITET_SOURCE_KEY",
    "CleanupError",
    "apply_cleanup",
    "build_cleanup_plan",
]
