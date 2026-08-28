"""Audited, idempotent merchant updates supplied on 2026-08-28.

The source images are not redistributed.  Each accepted table row is anchored
by its SHA-256 digest and one-based visual row number.  Ambiguous rows are
intentionally absent from this package.
"""

# Merchant data intentionally contains mixed Cyrillic and Latin brand names.
# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .stores import StoreError, StoreRepository


@dataclass(frozen=True, slots=True)
class ExistingVariant:
    """Expected production merchant used as a drift-check anchor."""

    merchant_id: int
    name: str
    channel: str = "offline"


@dataclass(frozen=True, slots=True)
class NewVariant:
    """One user-confirmed payment variant, optionally grouped with a brand."""

    key: str
    name: str
    channel: str
    group_with: str | None = None


@dataclass(frozen=True, slots=True)
class Confirmation:
    """One accepted image row supporting a merchant/channel/MCC fact."""

    variant: str
    mcc: str
    image_sha256: str
    row: int
    note: str = ""

    @property
    def source_key(self) -> str:
        """Return the deterministic evidence identity used for safe reruns."""

        return f"sha256:{self.image_sha256.lower()}:row:{self.row}"


IMAGE_OLD_1 = "31cb60cf3e17d2a61580403c8d2038d18924c5a321f2daf6e8f85c1be124587a"
IMAGE_OLD_2 = "1e01be1bf1999290e8a8afbe2512dc3c92362f086323fefe926b0cc1a1177fd3"
IMAGE_NEW = "9e4298a5f94d36f12752fa2acd0c5d77c18ead12b49e28eb5eac62b7456c3a4e"


EXISTING_VARIANTS: dict[str, ExistingVariant] = {
    "euroopt": ExistingVariant(18, "Евроопт"),
    "mila": ExistingVariant(7, "Мила"),
    "green": ExistingVariant(29, "Green"),
    "waikiki": ExistingVariant(38, "LC WAIKIKI"),
    "sportmaster": ExistingVariant(62, "Спортмастер"),
    "neighbors": ExistingVariant(65, "Соседи"),
    "dobrotsen": ExistingVariant(79, "Доброцен"),
    "gippo": ExistingVariant(88, "ГИППО"),
    "mak": ExistingVariant(89, "Mak.by"),
    "petruha": ExistingVariant(97, "Петруха"),
    "fix_price": ExistingVariant(102, "Fix Price"),
    "europochta": ExistingVariant(103, "Европочта"),
    "dionis": ExistingVariant(173, "Дионис"),
    "belarusneft": ExistingVariant(178, "АЗС Беларуснефть"),
    "kari": ExistingVariant(180, "Kari"),
    "three_prices": ExistingVariant(186, "Три цены"),
    "unistore": ExistingVariant(202, "Unistore опт&розница"),
    "svetofor": ExistingVariant(221, "Светофор"),
    "mayak": ExistingVariant(230, "Маяк"),
    "dary": ExistingVariant(245, "Дары от Зари"),
    "belita": ExistingVariant(265, "Белита-витэкс"),
    "milavitsa": ExistingVariant(292, "Милавица"),
    "emall": ExistingVariant(308, "emall.by", "online"),
    "groshyk": ExistingVariant(380, "Грошык"),
    "oma": ExistingVariant(393, "ОМА"),
    "red_food": ExistingVariant(438, "Красный пищевик"),
    "kommunarka": ExistingVariant(467, "Коммунарка"),
    "amatista": ExistingVariant(556, "Аматиста"),
    "spartak": ExistingVariant(619, "Спартак"),
    "mastak": ExistingVariant(645, "Мастак"),
    "seven_fridays": ExistingVariant(654, "Семь пятниц"),
    "ostin": ExistingVariant(658, "O'stin"),
    "marko": ExistingVariant(668, "Марко"),
    "motorlend": ExistingVariant(790, "Motorlend"),
    "optika_med": ExistingVariant(829, "Оптика-Медтехника"),
    "ozon": ExistingVariant(877, "OZON Беларусь", "online"),
    "ksk": ExistingVariant(888, "Строймаркет KSK", "online"),
}


# Exact duplicate payment variants.  Source merchant rows remain as archived
# merge lineage, while their source/fact/evidence/audit identifiers survive.
MERCHANT_MERGES: tuple[tuple[int, int, str, str], ...] = (
    (778, 7, "Мила", "Мила"),
    (588, 88, "Гиппо", "ГИППО"),
    (817, 88, "Гиппо", "ГИППО"),
    (574, 29, "Green", "Green"),
    (534, 178, "АЗС Беларуснефть", "АЗС Беларуснефть"),
    (608, 654, "Семь пятниц", "Семь пятниц"),
    (823, 654, "Семь пятниц", "Семь пятниц"),
)


NEW_VARIANTS: tuple[NewVariant, ...] = (
    NewVariant("familia", "Familia", "offline"),
    NewVariant("fish_thursday", "Рыбный «Четверг» (Гомель)", "offline"),
    NewVariant("tropik", "Тропик", "offline"),
    NewVariant("antresol", "Антресоль (Гомель)", "offline"),
    NewVariant("two_geese", "2 Гуся (Гомель)", "offline"),
    NewVariant("emall_offline", "eMall", "offline", "emall"),
    NewVariant("fishday_offline", "Fishday", "offline"),
    NewVariant("fishday_online", "Fishday", "online", "fishday_offline"),
    NewVariant("kabanchik", "Кабанчик мясо (Гомель)", "offline"),
    NewVariant("zhlobin_meat", "Жлобинский мясокомбинат", "offline"),
    NewVariant("slodych", "Слодыч", "offline"),
    NewVariant("rybka", "Сеть рыбных «Рыбка»", "offline"),
    NewVariant("nizkotsen", "Низкоцен", "offline"),
    NewVariant("nts", "Автоцентр НТС (Гомель)", "offline"),
    NewVariant("neostudio", "Неостудия (Гомель)", "offline"),
    NewVariant("beauty_home", "Beauty Home by Elena Mylnikova (Гомель)", "offline"),
    NewVariant("flowwow", "Flowwow", "online"),
    NewVariant("atlant_gomel", "АТЛАНТ (Гомель)", "offline"),
    NewVariant("ksk_offline", "Строймаркет KSK", "offline", "ksk"),
    NewVariant("blesk", "Блеск (Гомель)", "offline"),
    NewVariant("all_home", "Всё в дом (Гомель)", "offline"),
    NewVariant("letoile", "Л’Этуаль", "offline"),
    NewVariant("timosha", "Тимоша (Гомель)", "offline"),
    NewVariant("melange", "Меланж (Гомель)", "offline"),
    NewVariant("domtkani", "Домткани (Гомель)", "offline"),
)


# Metadata is applied to the brand that contains the referenced variant.
BRAND_METADATA: dict[str, tuple[str, tuple[str, ...]]] = {
    "familia": ("Familia", ("Famila",)),
    "emall": ("eMall", ("Емолл",)),
    "groshyk": ("Грошык", ("Грошик",)),
    "svetofor": ("Светофор", ("Светоф",)),
    "waikiki": ("LC WAIKIKI", ("Вайкики",)),
    "belarusneft": ("АЗС Беларуснефть", ("Беларуснефть",)),
    "ozon": ("OZON Беларусь", ("OZON",)),
    "mak": ("Mak.by", ("Мак бай",)),
    "three_prices": ("Три цены", ("3 цены", "3цены")),
    "fix_price": ("Fix Price", ("Фикс-Прайс", "Фикспрайс")),
    "unistore": ("Unistore опт&розница", ("Юнисторе",)),
    "belita": ("Белита-Витэкс", ("Белита",)),
    "seven_fridays": ("Семь пятниц", ("7пятниц",)),
    "zhlobin_meat": ("Жлобинский мясокомбинат", ("Жлобинский",)),
    "ksk": ("Строймаркет KSK", ("КСК",)),
    "letoile": ("Л’Этуаль", ("Летуаль",)),
}


CONFIRMATIONS: tuple[Confirmation, ...] = (
    # First earlier table: accepted, unambiguous rows only.
    Confirmation("euroopt", "5411", IMAGE_OLD_1, 1),
    Confirmation("groshyk", "5411", IMAGE_OLD_1, 3),
    Confirmation("svetofor", "5411", IMAGE_OLD_1, 6),
    Confirmation("dionis", "5411", IMAGE_OLD_1, 7),
    Confirmation("spartak", "5441", IMAGE_OLD_1, 8),
    Confirmation("marko", "5661", IMAGE_OLD_1, 9),
    Confirmation("oma", "5200", IMAGE_OLD_1, 11),
    Confirmation("ksk_offline", "5211", IMAGE_OLD_1, 12),
    Confirmation("mastak", "5211", IMAGE_OLD_1, 13),
    Confirmation("waikiki", "5651", IMAGE_OLD_1, 15),
    Confirmation("sportmaster", "5941", IMAGE_OLD_1, 16),
    # Second earlier table.
    Confirmation("belarusneft", "5541", IMAGE_OLD_2, 1),
    Confirmation("ozon", "5262", IMAGE_OLD_2, 2),
    Confirmation("mak", "5814", IMAGE_OLD_2, 8),
    Confirmation("mila", "5999", IMAGE_OLD_2, 11),
    Confirmation("three_prices", "5399", IMAGE_OLD_2, 13),
    Confirmation("fix_price", "5399", IMAGE_OLD_2, 14),
    # New 60-row table. Rows 23, 24, 37, 39, 45, 56 and 57 are held back.
    Confirmation("familia", "5311", IMAGE_NEW, 1),
    Confirmation("neighbors", "5411", IMAGE_NEW, 2),
    Confirmation("fish_thursday", "5499", IMAGE_NEW, 3),
    Confirmation("tropik", "5411", IMAGE_NEW, 4),
    Confirmation("euroopt", "5411", IMAGE_NEW, 5),
    Confirmation("antresol", "5411", IMAGE_NEW, 6),
    Confirmation("two_geese", "5411", IMAGE_NEW, 7),
    Confirmation("green", "5411", IMAGE_NEW, 8),
    Confirmation("gippo", "5411", IMAGE_NEW, 9),
    Confirmation(
        "emall_offline",
        "5411",
        IMAGE_NEW,
        10,
        "Оплата товаров eMall на Европочте",
    ),
    Confirmation("emall", "5300", IMAGE_NEW, 11, "Онлайн-оплата на eMall"),
    Confirmation("emall", "4215", IMAGE_NEW, 12, "Сторонние товары"),
    Confirmation("mayak", "5411", IMAGE_NEW, 13),
    Confirmation("dary", "5411", IMAGE_NEW, 14),
    Confirmation("dobrotsen", "5411", IMAGE_NEW, 15),
    Confirmation("spartak", "5441", IMAGE_NEW, 16),
    Confirmation("fishday_offline", "5499", IMAGE_NEW, 17, "Фирменный магазин в Гомеле"),
    Confirmation(
        "fishday_online",
        "5499",
        IMAGE_NEW,
        18,
        "Оплата ЕРИП при доставке курьером",
    ),
    Confirmation("petruha", "5411", IMAGE_NEW, 19),
    Confirmation("unistore", "5411", IMAGE_NEW, 20),
    Confirmation("svetofor", "5411", IMAGE_NEW, 21),
    Confirmation("kabanchik", "5422", IMAGE_NEW, 22),
    Confirmation("zhlobin_meat", "5422", IMAGE_NEW, 25, "Центральный рынок"),
    Confirmation(
        "zhlobin_meat",
        "5411",
        IMAGE_NEW,
        26,
        "Фирменный, ул. 60 лет СССР",
    ),
    Confirmation("slodych", "5441", IMAGE_NEW, 27),
    Confirmation("kommunarka", "5411", IMAGE_NEW, 28),
    Confirmation("red_food", "5499", IMAGE_NEW, 29),
    Confirmation("rybka", "5921", IMAGE_NEW, 30),
    Confirmation("ostin", "5651", IMAGE_NEW, 31),
    Confirmation("milavitsa", "5621", IMAGE_NEW, 32),
    Confirmation("nizkotsen", "5621", IMAGE_NEW, 33),
    Confirmation("kari", "5661", IMAGE_NEW, 34),
    Confirmation("belita", "5651", IMAGE_NEW, 35),
    Confirmation("nts", "5511", IMAGE_NEW, 36),
    Confirmation("optika_med", "8043", IMAGE_NEW, 38),
    Confirmation("neostudio", "7230", IMAGE_NEW, 40),
    Confirmation("three_prices", "5399", IMAGE_NEW, 41),
    Confirmation("motorlend", "5931", IMAGE_NEW, 42),
    Confirmation("beauty_home", "7230", IMAGE_NEW, 43),
    Confirmation("mila", "5999", IMAGE_NEW, 44),
    Confirmation("fix_price", "5399", IMAGE_NEW, 46),
    Confirmation("flowwow", "5992", IMAGE_NEW, 47),
    Confirmation("atlant_gomel", "7629", IMAGE_NEW, 48),
    Confirmation("europochta", "4215", IMAGE_NEW, 49),
    Confirmation("ksk_offline", "5211", IMAGE_NEW, 50),
    Confirmation("blesk", "5977", IMAGE_NEW, 51),
    Confirmation("seven_fridays", "5921", IMAGE_NEW, 52),
    Confirmation("amatista", "5399", IMAGE_NEW, 53),
    Confirmation("all_home", "5399", IMAGE_NEW, 54),
    Confirmation("letoile", "5977", IMAGE_NEW, 55),
    Confirmation("timosha", "5995", IMAGE_NEW, 58),
    Confirmation("melange", "5949", IMAGE_NEW, 59),
    Confirmation("domtkani", "5714", IMAGE_NEW, 60),
)


HELD_ROWS: dict[str, tuple[int, ...]] = {
    IMAGE_OLD_1: (2, 4, 5, 10, 14),
    IMAGE_OLD_2: (3, 4, 5, 6, 7, 9, 10, 12),
    IMAGE_NEW: (23, 24, 37, 39, 45, 56, 57),
}


class CuratedUpdateError(StoreError):
    """The target database drifted from the reviewed production snapshot."""


def _merchant_row(connection, merchant_id: int):
    row = connection.execute(
        "SELECT id,name,channel,archived,merged_into FROM store_merchants WHERE id=?",
        (merchant_id,),
    ).fetchone()
    if row is None:
        raise CuratedUpdateError(f"Ожидаемый вариант #{merchant_id} не найден")
    return row


def _preflight_existing(connection) -> dict[str, int]:
    variants: dict[str, int] = {}
    for key, expected in EXISTING_VARIANTS.items():
        row = _merchant_row(connection, expected.merchant_id)
        if (
            row["name"] != expected.name
            or row["channel"] != expected.channel
            or row["archived"]
            or row["merged_into"] is not None
        ):
            raise CuratedUpdateError(f"Вариант #{expected.merchant_id} изменился после проверки")
        variants[key] = expected.merchant_id
    return variants


def _merge_duplicates(repository, connection, actor_id: int) -> tuple[int, int]:
    changed = already = 0
    for source_id, target_id, source_name, target_name in MERCHANT_MERGES:
        source = _merchant_row(connection, source_id)
        target = _merchant_row(connection, target_id)
        if target["name"] != target_name or target["archived"]:
            raise CuratedUpdateError(f"Целевой вариант #{target_id} изменился после проверки")
        if source["merged_into"] == target_id and source["archived"]:
            already += 1
            continue
        if source["name"] != source_name or source["archived"] or source["merged_into"]:
            raise CuratedUpdateError(f"Дубликат #{source_id} изменился после проверки")
        repository.apply_change(
            "merge_merchant",
            {"merchant_id": source_id, "target_id": target_id},
            actor_id,
            connection=connection,
        )
        changed += 1
    return changed, already


def _ensure_variants(
    repository, connection, actor_id: int, variants: dict[str, int]
) -> tuple[int, int]:
    created = existing = 0
    for item in NEW_VARIANTS:
        matches = repository.find_exact(item.name, item.channel, connection=connection)
        if len(matches) > 1:
            raise CuratedUpdateError(f"Нельзя однозначно продолжить: {item.name} / {item.channel}")
        brand_id = None
        if item.group_with is not None:
            group = repository.brand_for_merchant(variants[item.group_with], connection=connection)
            if group is None:
                raise CuratedUpdateError(f"Бренд для {item.group_with} не найден")
            brand_id = group.id
        if matches:
            merchant_id = matches[0].id
            brand = repository.brand_for_merchant(merchant_id, connection=connection)
            if brand is None or (brand_id is not None and brand.id != brand_id):
                raise CuratedUpdateError(f"Вариант {item.name} уже принадлежит другому бренду")
            existing += 1
        else:
            payload = {"name": item.name, "channel": item.channel}
            if brand_id is not None:
                payload["brand_id"] = brand_id
            result = repository.apply_change(
                "add_merchant", payload, actor_id, connection=connection
            )
            merchant_id = result.merchant_id
            created += 1
        variants[item.key] = merchant_id
    return created, existing


def _ensure_brand_metadata(
    repository, connection, actor_id: int, variants: dict[str, int]
) -> tuple[int, int]:
    changed = already = 0
    for key, (name, requested_aliases) in BRAND_METADATA.items():
        brand = repository.brand_for_merchant(variants[key], connection=connection)
        if brand is None:
            raise CuratedUpdateError(f"Бренд для {key} не найден")
        if brand.name != name:
            brand_id = brand.id
            repository.apply_change(
                "rename_brand",
                {"brand_id": brand_id, "name": name},
                actor_id,
                connection=connection,
            )
            changed += 1
            brand = repository.get_brand(brand_id, connection=connection)
            if brand is None:
                raise CuratedUpdateError(f"Бренд #{brand_id} исчез после переименования")
        aliases = tuple(dict.fromkeys((*brand.aliases, *requested_aliases)))
        if aliases != brand.aliases:
            repository.apply_change(
                "brand_aliases",
                {"brand_id": brand.id, "aliases": aliases},
                actor_id,
                connection=connection,
            )
            changed += 1
        elif brand.name == name:
            already += 1
    return changed, already


def apply_curated_update(repository: StoreRepository, actor_id: int) -> dict[str, int]:
    """Apply the reviewed package atomically and return privacy-safe counters."""

    if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id < 1:
        raise CuratedUpdateError("Нужен положительный ID владельца для аудита")
    with repository.transaction() as connection:
        variants = _preflight_existing(connection)
        merges, merges_existing = _merge_duplicates(repository, connection, actor_id)
        variants_created, variants_existing = _ensure_variants(
            repository, connection, actor_id, variants
        )
        metadata_changed, metadata_existing = _ensure_brand_metadata(
            repository, connection, actor_id, variants
        )
        confirmations_added = confirmations_existing = 0
        for item in CONFIRMATIONS:
            result = repository.confirm_mcc(
                variants[item.variant],
                item.mcc,
                actor_id=actor_id,
                source="curated-image",
                source_key=item.source_key,
                evidence={"source": "user-provided-image", "occurrence": item.row},
                note=item.note or None,
                connection=connection,
            )
            if result.audit_id:
                confirmations_added += 1
            else:
                confirmations_existing += 1
        return {
            "merges_applied": merges,
            "merges_existing": merges_existing,
            "variants_created": variants_created,
            "variants_existing": variants_existing,
            "metadata_changes": metadata_changed,
            "metadata_existing": metadata_existing,
            "confirmations_added": confirmations_added,
            "confirmations_existing": confirmations_existing,
            "held_rows": sum(len(rows) for rows in HELD_ROWS.values()),
        }


def main(argv: list[str] | None = None) -> None:
    """Apply this exact package to the configured merchant database."""

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("MCC_STORES_PATH", "var/stores.sqlite3")),
    )
    args = parser.parse_args(argv)
    raw_actor = os.environ.get("BOT_OWNER_TELEGRAM_ID", "")
    try:
        actor_id = int(raw_actor)
    except ValueError as exc:
        raise SystemExit("BOT_OWNER_TELEGRAM_ID must be a positive integer") from exc
    repository = StoreRepository(args.database)
    repository.initialize()
    print(json.dumps(apply_curated_update(repository, actor_id), sort_keys=True))


if __name__ == "__main__":
    main()
