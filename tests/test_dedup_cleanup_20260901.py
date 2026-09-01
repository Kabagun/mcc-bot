# ruff: noqa: RUF001

from __future__ import annotations

import json
from pathlib import Path

from mcc_bot.dedup_cleanup_20260901 import (
    DELETED_PARTNER_SOURCE_KEYS,
    DELETED_TANNEI_SOURCE_IDS,
    apply_cleanup,
    build_cleanup_plan,
)
from mcc_bot.partner_rewards import PartnerRepository
from mcc_bot.stores import StoreRepository


def _brand(
    connection,
    brand_id: int,
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    revision: int = 1,
) -> None:
    connection.execute(
        "INSERT INTO store_brands(id,name,aliases_json,revision) VALUES(?,?,?,?)",
        (brand_id, name, json.dumps(list(aliases), ensure_ascii=False), revision),
    )


def _merchant(
    connection,
    merchant_id: int,
    name: str,
    channel: str,
    brand_id: int,
    *,
    source_identity: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO store_merchants(id,name,channel,aliases_json,source_identity) "
        "VALUES(?,?,?,'[]',?)",
        (merchant_id, name, channel, source_identity),
    )
    connection.execute(
        "INSERT INTO store_brand_members(brand_id,merchant_id) VALUES(?,?)",
        (brand_id, merchant_id),
    )


def _fact(connection, fact_id: int, merchant_id: int, mcc: str, note: str = "") -> None:
    connection.execute(
        "INSERT INTO store_facts(id,merchant_id,mcc,note) VALUES(?,?,?,?)",
        (fact_id, merchant_id, mcc, note),
    )


def _offer(
    connection,
    offer_id: int,
    source_key: str | None,
    brand_id: int,
    card_id: str,
    channel: str,
    value: str,
    *,
    tier_id: int | None = None,
    reward_kind: str = "cash",
    conditions: str = "",
    source_url: str = "",
) -> None:
    connection.execute(
        """INSERT INTO partner_offers
        (id,source_key,brand_id,card_id,channel,mode,reward_kind,conditions,source_url,created_by,updated_by)
        VALUES(?,?,?,?,?,'total', ?, ?, ?,1,1)""",
        (offer_id, source_key, brand_id, card_id, channel, reward_kind, conditions, source_url),
    )
    connection.execute(
        """INSERT INTO partner_offer_tiers
        (id,offer_id,position,value) VALUES(?,?,0,?)""",
        (tier_id or offer_id, offer_id, value),
    )


def _fixture(tmp_path: Path) -> StoreRepository:
    repository = StoreRepository(tmp_path / "stores.sqlite3")
    repository.initialize()
    PartnerRepository(repository).initialize()
    brand_names = {
        70: "21vek.by",
        640: "21vek.by",
        1025: "21vek",
        1067: "Unistore",
        202: "Unistore опт&розница",
        510: "Mooon",
        660: "Mooon",
        699: "Mooon",
        4: "mooon.by",
        711: "Kakvapteke.by",
        1051: "КАК В АПТЕКЕ",
        945: "Многофункциональный комплекс «Мандарин»",
        821: "МФК «Мандарин»",
        790: "Motorlend",
        179: "Motorland",
        595: "Papa Doner",
        453: "Papa Doner",
        773: "Salateira",
        613: "Salateira",
        798: "Компьютерный Мир",
        845: "Компьютерный мир",
        589: "Мода Макс",
        505: "МодаМакс",
        858: "lamoda",
        181: "lamoda",
        892: "life:)",
        465: "life:)",
        137: "Elgato.by",
        61: "El Gato",
        729: "oz.by",
        206: "OZ",
        646: "7745.by",
        207: "7745 Большой магазин",
        246: "conteshop.by",
        218: "Conte",
        789: "officetonmarket.by",
        287: "Офистон Маркет",
        641: "pass.rw.by",
        22: "Белорусская железная дорога",
        679: "Приложение Дзякуй",
        78: "A-100",
        311: "belpost.by",
        239: "Белпочта",
        677: "Меридиан",
        10: "Меридиан",
        635: "Кофе-автомат",
        583: "Кофе-автомат",
        383: "Услуги связи",
        498: "Услуги связи",
        515: "Услуги связи",
        508: "Мирум",
        714: "Мирум",
        944: 'Отели "Аква-Минск"',
    }
    online = {
        383,
        498,
        515,
        640,
        1028,
        711,
        4,
        892,
        137,
        729,
        646,
        246,
        789,
        641,
        679,
        311,
    }
    with repository.transaction() as connection:
        for brand_id, name in brand_names.items():
            aliases = {202: ("Юнисторе",), 1067: ("Uni store",), 945: ("Мандарин",)}
            revision = 2 if brand_id == 202 else 1
            _brand(connection, brand_id, name, aliases=aliases.get(brand_id, ()), revision=revision)
        # Every brand has one merchant except the 21vek brand, which has both
        # channels in the reviewed package.
        for brand_id, name in brand_names.items():
            if brand_id == 1025:
                _merchant(connection, 1028, name, "online", brand_id)
                _merchant(connection, 1074, name, "offline", brand_id)
            else:
                merchant_id = {944: 947, 945: 948}.get(brand_id, brand_id)
                identities = {
                    677: "tannei:store:1728829",
                    10: "tannei:store:1008029",
                    635: "tannei:store:1664492",
                    583: "tannei:store:1599168",
                }
                _merchant(
                    connection,
                    merchant_id,
                    name,
                    "online" if brand_id in online else "offline",
                    brand_id,
                    source_identity=identities.get(merchant_id),
                )
                if merchant_id == 948:
                    connection.execute(
                        "UPDATE store_merchants SET aliases_json=? WHERE id=948",
                        (json.dumps(["Мандарин"], ensure_ascii=False),),
                    )
        _fact(connection, 70, 70, "5300")
        _fact(connection, 360, 70, "5311")
        _fact(connection, 687, 640, "5300")
        _fact(connection, 688, 640, "5399")
        _fact(connection, 689, 640, "5311")
        _fact(connection, 1002, 1028, "5300", "Оплата в интернете")
        _fact(connection, 1006, 1074, "5399", "Курьеру")
        _fact(connection, 729, 677, "5499")
        _fact(connection, 10, 10, "5499")
        _fact(connection, 682, 635, "5812")
        _fact(connection, 626, 583, "5812")
        _offer(
            connection,
            30,
            DELETED_PARTNER_SOURCE_KEYS[0],
            944,
            "cactus_mtbank",
            "any",
            "1.5",
            reward_kind="points",
        )
        _offer(
            connection,
            31,
            DELETED_PARTNER_SOURCE_KEYS[1],
            944,
            "cactus_mtbank",
            "any",
            "1.5",
            reward_kind="points",
        )
        _offer(
            connection,
            33,
            DELETED_PARTNER_SOURCE_KEYS[2],
            944,
            "cactus_mtbank",
            "any",
            "1.5",
            reward_kind="points",
        )
        _offer(
            connection,
            35,
            DELETED_PARTNER_SOURCE_KEYS[3],
            944,
            "cactus_mtbank",
            "any",
            "1.5",
            reward_kind="points",
        )
        _offer(
            connection,
            32,
            "cactus:mandarin",
            945,
            "cactus_mtbank",
            "any",
            "1.5",
            reward_kind="points",
        )
        _offer(
            connection,
            115,
            "cashalot:21vek-by",
            640,
            "belgazprombank_cashalot",
            "online",
            "1.5",
            conditions="Повышенный кэшбэк в партнёрской сети Cashalot.",
            source_url="https://cashalot.by/stores/store_select/21vek-by/",
        )
        _offer(connection, 138, "bnb-1-2-3:a72c0ccd", 1025, "bnb_1_2_3", "online", "2")
        _offer(connection, 177, "plushki:promo:01", 1025, "vitamin_d", "online", "5")
        _offer(
            connection,
            205,
            "statuskarta:21vek:2026-08-30",
            1025,
            "statusbank_statuskarta",
            "online",
            "2.5",
        )
        _offer(connection, 207, None, 1025, "belgazprombank_cashalot", "any", "1.01", tier_id=210)
        for seed_id, source_key, brand_id in (
            (30, "brand:2b75223b7ce41437e08a", 944),
            (31, "brand:69f8d947608c584ffa1e", 944),
            (33, "brand:fa327563361a72956660", 944),
            (35, "brand:c8be57618f0c48e39a91", 944),
            (32, "brand:mandarin", 945),
            (115, "brand:cashalot:21vek-by", 640),
            (138, "brand:bnb:a1799d4e", 1025),
            (176, "brand:plushki:1cf0c01d", 1025),
            (204, "brand:statuskarta:21vek", 1025),
        ):
            connection.execute(
                "INSERT INTO partner_seed_brands(id,source_key,brand_id) VALUES(?,?,?)",
                (seed_id, source_key, brand_id),
            )
        connection.execute(
            "INSERT INTO store_audit(id,kind,merchant_id,brand_id,actor_id,changes_json) "
            "VALUES(1638,'add_merchant',947,944,1,'[]')"
        )
        for offer_id in (30, 31, 33, 35):
            connection.execute(
                "INSERT INTO partner_audit(id,entity_type,entity_id,action,actor_id,snapshot_json) "
                "VALUES(?,?,?,?,?,?)",
                (offer_id, "offer", offer_id, "create", 1, "{}"),
            )
        for source_id, _brand_id, merchant_id, _name, channel, identity in (
            ("1391019", 383, 383, "Услуги связи", "online", "tannei:online:1391019"),
            ("1509980", 498, 498, "Услуги связи", "online", "tannei:online:1509980"),
            ("1534752", 515, 515, "Услуги связи", "online", "tannei:online:1534752"),
            ("1520712", 508, 508, "Мирум", "offline", "tannei:store:1520712"),
            ("1770238", 714, 714, "Мирум", "offline", "tannei:store:1770238"),
        ):
            source_row_id = {
                "1391019": 539,
                "1509980": 743,
                "1534752": 778,
                "1520712": 757,
                "1770238": 1135,
            }[source_id]
            metadata = json.dumps(
                {
                    "address": (
                        None
                        if source_id in {"1391019", "1509980", "1534752"}
                        else (
                            "Гродненская область, Кореличский район, "
                            "Мирский сельский Совет, Мир, Красноармейская улица, 1А"
                        )
                    ),
                    "id": int(source_id),
                    "is_online": channel == "online",
                    "name": _name,
                    "network_id": None,
                    "network_name": None,
                }
            )
            connection.execute(
                "INSERT INTO store_sources("
                "id,source,store_id,merchant_id,metadata_json) "
                "VALUES(?, 'tannei', ?, ?, ?)",
                (source_row_id, source_id, merchant_id, metadata),
            )
            connection.execute(
                "UPDATE store_merchants SET source_identity=? WHERE id=?",
                (identity, merchant_id),
            )
            connection.execute(
                "INSERT INTO store_tannei_import_guards("
                "id,store_source_id,source_identity,revision,fingerprint) "
                "VALUES(?,?,?,1,'x')",
                (source_row_id, source_row_id, identity),
            )
        for source_row_id, source_id, merchant_id in (
            (878, "1599168", 583),
            (978, "1664492", 635),
        ):
            connection.execute(
                "INSERT INTO store_sources("
                "id,source,store_id,merchant_id,metadata_json) "
                "VALUES(?, 'tannei', ?, ?, ?)",
                (
                    source_row_id,
                    source_id,
                    merchant_id,
                    json.dumps(
                        {
                            "address": "Брестская область, Кобрин, улица Николаева, 50",
                            "id": int(source_id),
                            "is_online": False,
                            "name": "Кофе-автомат",
                            "network_id": None,
                            "network_name": None,
                        }
                    ),
                ),
            )
        for source_row_id, source_id, merchant_id in (
            (879, "1008029", 10),
            (979, "1728829", 677),
        ):
            connection.execute(
                "INSERT INTO store_sources("
                "id,source,store_id,merchant_id,metadata_json) "
                "VALUES(?, 'tannei', ?, ?, ?)",
                (
                    source_row_id,
                    source_id,
                    merchant_id,
                    json.dumps(
                        {
                            "address": "Брестская область, Кобрин, улица Дзержинского, 45А",
                            "id": int(source_id),
                            "is_online": False,
                            "name": "Меридиан",
                            "network_id": None,
                            "network_name": None,
                        }
                    ),
                ),
            )
        _fact(connection, 408, 383, "6012")
        _fact(connection, 533, 498, "4814")
        _fact(connection, 534, 498, "6012")
        _fact(connection, 535, 498, "4900")
        _fact(connection, 545, 508, "5812")
        _fact(connection, 554, 515, "6012")
        _fact(connection, 555, 515, "4900")
        _fact(connection, 772, 714, "5411")
    return repository


def test_cleanup_is_dry_run_by_default_and_apply_is_idempotent(tmp_path: Path) -> None:
    repository = _fixture(tmp_path)
    before = repository.counts()
    plan = build_cleanup_plan(repository)

    assert plan["counts"]["would_merge"] == 25
    assert set(plan["would_add"]["tannei_tombstones"]) == set(DELETED_TANNEI_SOURCE_IDS)
    assert set(plan["would_add"]["partner_tombstones"]) == set(DELETED_PARTNER_SOURCE_KEYS)
    assert plan["would_add"]["offers"][0]["card_id"] == "paritet_combo"
    assert repository.counts() == before

    first = apply_cleanup(repository, actor_id=1, dry_run=False)
    second = apply_cleanup(repository, actor_id=1, dry_run=False)

    assert first["applied"]["merged"] == {"brand": 23, "merchant": 2}
    assert first["applied"]["added"]["offers"] == 1
    assert second["applied"]["merged"] == {"brand": 0, "merchant": 0}
    assert second["applied"]["added"]["offers"] == 0
    with repository.connection() as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT name,aliases_json FROM store_brands WHERE id=202"
            ).fetchone()[0]
            == "Unistore"
        )
        assert (
            connection.execute("SELECT aliases_json FROM store_brands WHERE id=821").fetchone()[0]
            == '["Мандарин"]'
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM partner_offers WHERE brand_id=1025 AND archived=0"
            ).fetchone()[0]
            == 5
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM store_sources WHERE store_id IN (?,?,?,?,?)",
                DELETED_TANNEI_SOURCE_IDS,
            ).fetchone()[0]
            == 0
        )
