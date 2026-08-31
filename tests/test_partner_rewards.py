from __future__ import annotations

# Russian user-facing copy is intentional.
# ruff: noqa: RUF001
import json
from datetime import date
from decimal import Decimal

import pytest

from mcc_bot.catalog import CardCatalog
from mcc_bot.formatting import format_matches, format_moneyback
from mcc_bot.partner_rewards import (
    PartnerExclusionInput,
    PartnerOfferInput,
    PartnerRepository,
    PartnerTierInput,
    resolve_store_matches,
)
from mcc_bot.stores import StoreError, StoreRepository


def _catalog(tmp_path) -> CardCatalog:
    path = tmp_path / "partner-cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "cards": [
                    {
                        "id": "vitamin_d",
                        "name": "Витамин Д",
                        "issuer": "Беларусбанк",
                        "emoji": "💊",
                        "reward_programs": [
                            {
                                "id": "vitamin-cash",
                                "kind": "cash",
                                "tax_exempt": False,
                                "offers": [
                                    {"mcc": "4111", "value": 1},
                                    {"mcc": "5411", "value": 1},
                                ],
                            },
                            {
                                "id": "vitamin-points",
                                "kind": "points",
                                "tax_exempt": True,
                                "offers": [
                                    {"mcc": "1234", "value": 3},
                                    {"mcc": "4111", "value": 3},
                                    {"mcc": "5411", "value": 3},
                                ],
                            },
                        ],
                    },
                    {
                        "id": "other",
                        "name": "Другая карта",
                        "issuer": "Другой банк",
                        "emoji": "💳",
                        "reward_programs": [
                            {
                                "kind": "cash",
                                "tax_exempt": False,
                                "offers": [{"mcc": "1234", "value": 7}],
                            }
                        ],
                    },
                    {
                        "id": "cactus_mtbank",
                        "name": "Кактус",
                        "issuer": "МТБанк",
                        "emoji": "🌵",
                        "reward_programs": [
                            {
                                "kind": "cash",
                                "tax_exempt": False,
                                "offers": [{"mcc": "5411", "value": 1}],
                            }
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return CardCatalog.from_file(path)


def _repositories(tmp_path, name="21 век"):
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    result = stores.apply_change(
        "add_merchant", {"name": name, "channel": "online", "mcc": "1234"}, 1
    )
    partners = PartnerRepository(stores)
    partners.initialize()
    assert result.brand_id is not None
    return stores, partners, result.brand_id


def test_initialize_migrates_legacy_seed_brand_bindings_to_stable_ids(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    brand_id = stores.apply_change(
        "add_merchant", {"name": "Legacy seed store", "channel": "offline", "mcc": "5411"}, 1
    ).brand_id
    assert brand_id is not None
    with stores.transaction() as connection:
        connection.execute(
            """CREATE TABLE partner_seed_brands (
            source_key TEXT PRIMARY KEY,
            brand_id INTEGER NOT NULL REFERENCES store_brands(id))"""
        )
        connection.execute(
            "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?)",
            ("legacy:store", brand_id),
        )

    partners = PartnerRepository(stores)
    partners.initialize()
    with stores.connection() as connection:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(partner_seed_brands)")
        }
        row = connection.execute(
            "SELECT id,source_key,brand_id FROM partner_seed_brands"
        ).fetchone()

    assert columns >= {"id", "source_key", "brand_id"}
    assert row is not None
    assert row["id"] > 0
    assert row["source_key"] == "legacy:store"
    assert row["brand_id"] == brand_id


def test_initialize_recovers_interrupted_seed_binding_copy_without_data_loss(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    first = stores.apply_change(
        "add_merchant", {"name": "First", "channel": "offline", "mcc": "5411"}, 1
    )
    second = stores.apply_change(
        "add_merchant", {"name": "Second", "channel": "offline", "mcc": "5411"}, 1
    )
    assert first.brand_id is not None and second.brand_id is not None
    with stores.transaction() as connection:
        connection.execute(
            """CREATE TABLE partner_seed_brands (
            id INTEGER PRIMARY KEY, source_key TEXT NOT NULL UNIQUE,
            brand_id INTEGER NOT NULL REFERENCES store_brands(id))"""
        )
        connection.execute(
            """CREATE TABLE partner_seed_brands_legacy (
            source_key TEXT PRIMARY KEY,
            brand_id INTEGER NOT NULL REFERENCES store_brands(id))"""
        )
        connection.execute(
            "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?)",
            ("already-copied", first.brand_id),
        )
        connection.execute(
            "INSERT INTO partner_seed_brands_legacy(source_key,brand_id) VALUES(?,?)",
            ("not-copied-yet", second.brand_id),
        )

    PartnerRepository(stores).initialize()

    with stores.connection() as connection:
        rows = connection.execute(
            "SELECT source_key,brand_id FROM partner_seed_brands ORDER BY source_key"
        ).fetchall()
        legacy_exists = connection.execute(
            """SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='partner_seed_brands_legacy'"""
        ).fetchone()
    assert [(row["source_key"], row["brand_id"]) for row in rows] == [
        ("already-copied", first.brand_id),
        ("not-copied-yet", second.brand_id),
    ]
    assert legacy_exists is None


def test_brand_merge_rehomes_partner_rows_and_revert_restores_them(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    source = stores.apply_change(
        "add_merchant", {"name": "Source", "channel": "offline", "mcc": "5411"}, 1
    )
    target = stores.apply_change(
        "add_merchant", {"name": "Target", "channel": "offline", "mcc": "5411"}, 1
    )
    assert source.brand_id is not None and target.brand_id is not None
    partners = PartnerRepository(stores)
    partners.initialize()
    offer = partners.create_offer(
        _offer(source.brand_id, channel="offline"), actor_id=1, source_key="seed:offer"
    )
    exclusion = partners.create_exclusion(
        PartnerExclusionInput(
            brand_id=source.brand_id,
            card_id="vitamin_d",
            reward_kind="points",
            channel="offline",
            mcc="5411",
            starts_on=None,
            ends_on=None,
            suppress_base=False,
            reason="Test",
            source_url="",
        ),
        actor_id=1,
        source_key="seed:exclusion",
    )
    with stores.transaction() as connection:
        connection.execute(
            "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?)",
            ("seed:brand", source.brand_id),
        )

    merged = stores.apply_change(
        "merge_brand",
        {"brand_id": source.brand_id, "target_id": target.brand_id},
        1,
    )
    assert partners.get_offer(offer.id, include_archived=True).brand_id == target.brand_id
    moved_exclusion = next(
        item
        for item in partners.list_exclusions(
            target.brand_id, include_archived=True, include_global=False
        )
        if item.id == exclusion.id
    )
    assert moved_exclusion.brand_id == target.brand_id
    with stores.connection() as connection:
        assert (
            connection.execute(
                "SELECT brand_id FROM partner_seed_brands WHERE source_key='seed:brand'"
            ).fetchone()["brand_id"]
            == target.brand_id
        )

    stores.apply_change("revert", {"audit_id": merged.audit_id}, 1)
    assert partners.get_offer(offer.id, include_archived=True).brand_id == source.brand_id
    restored_exclusion = next(
        item
        for item in partners.list_exclusions(
            source.brand_id, include_archived=True, include_global=False
        )
        if item.id == exclusion.id
    )
    assert restored_exclusion.brand_id == source.brand_id
    with stores.connection() as connection:
        assert (
            connection.execute(
                "SELECT brand_id FROM partner_seed_brands WHERE source_key='seed:brand'"
            ).fetchone()["brand_id"]
            == source.brand_id
        )


def test_brand_merge_collapses_identical_partner_offers_and_revert_restores_them(
    tmp_path,
) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    source = stores.apply_change(
        "add_merchant", {"name": "Source", "channel": "offline", "mcc": "5411"}, 1
    )
    target = stores.apply_change(
        "add_merchant", {"name": "Target", "channel": "offline", "mcc": "5411"}, 1
    )
    assert source.brand_id is not None and target.brand_id is not None
    partners = PartnerRepository(stores)
    partners.initialize()
    source_offer = partners.create_offer(
        _offer(source.brand_id, channel="offline"), actor_id=1, source_key="seed:source"
    )
    target_offer = partners.create_offer(
        _offer(target.brand_id, channel="offline"), actor_id=1, source_key="seed:target"
    )

    merged = stores.apply_change(
        "merge_brand", {"brand_id": source.brand_id, "target_id": target.brand_id}, 1
    )

    moved = partners.get_offer(source_offer.id, include_archived=True)
    survivor = partners.get_offer(target_offer.id, include_archived=True)
    assert moved is not None and moved.brand_id == target.brand_id and moved.archived
    assert survivor is not None and not survivor.archived

    stores.apply_change("revert", {"audit_id": merged.audit_id}, 1)
    restored = partners.get_offer(source_offer.id, include_archived=True)
    assert restored is not None and restored.brand_id == source.brand_id and not restored.archived


def test_brand_merge_blocks_conflicting_partner_rates_without_partial_writes(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    source = stores.apply_change(
        "add_merchant", {"name": "Source", "channel": "offline", "mcc": "5411"}, 1
    )
    target = stores.apply_change(
        "add_merchant", {"name": "Target", "channel": "offline", "mcc": "5411"}, 1
    )
    assert source.brand_id is not None and target.brand_id is not None
    partners = PartnerRepository(stores)
    partners.initialize()
    source_offer = partners.create_offer(
        _offer(source.brand_id, channel="offline"), actor_id=1, source_key="seed:source"
    )
    target_offer = partners.create_offer(
        _offer(
            target.brand_id,
            channel="offline",
            tiers=(PartnerTierInput(Decimal("9")),),
        ),
        actor_id=1,
        source_key="seed:target",
    )

    with pytest.raises(StoreError, match="конфликтуют партнёрские условия"):
        stores.apply_change(
            "merge_brand", {"brand_id": source.brand_id, "target_id": target.brand_id}, 1
        )

    assert stores.get_brand(source.brand_id) is not None
    assert stores.get_brand(target.brand_id) is not None
    assert partners.get_offer(source_offer.id, include_archived=True).brand_id == source.brand_id
    assert partners.get_offer(target_offer.id, include_archived=True).brand_id == target.brand_id


def _offer(brand_id, **overrides) -> PartnerOfferInput:
    values = {
        "brand_id": brand_id,
        "card_id": "vitamin_d",
        "channel": "online",
        "mode": "additional",
        "reward_kind": "points",
        "tiers": (
            PartnerTierInput(
                Decimal("5"),
                min_purchase=Decimal("100"),
                per_transaction_cap=Decimal("50"),
            ),
        ),
        "starts_on": date(2026, 8, 1),
        "ends_on": date(2026, 10, 31),
        "conditions": "Только при онлайн-оплате",
        "source_url": "",
    }
    values.update(overrides)
    return PartnerOfferInput(**values)


def test_additional_points_are_composed_displayed_once_and_ranked(tmp_path) -> None:
    _stores, partners, brand_id = _repositories(tmp_path)
    partners.create_offer(_offer(brand_id), actor_id=1)
    catalog = _catalog(tmp_path)

    raw = catalog.lookup("1234")
    resolved = resolve_store_matches(
        catalog, partners, brand_id, "online", "1234", on_date=date(2026, 8, 30)
    )

    assert format_moneyback(raw[1]) == "3% баллами"
    assert resolved[0].card.id == "vitamin_d"
    assert resolved[0].gross_value == Decimal("8")
    assert format_moneyback(resolved[0]) == "3% + 5% баллами"
    rendered = format_matches("1234", resolved, {"1234": "Покупка"})
    assert "Витамин Д — 3% + 5% баллами" in rendered
    assert ("сумма от 100 BYN · не больше 50 баллов за операцию · по 31.10.2026") in rendered
    assert "Партнёрское предложение" not in rendered
    assert "Только при онлайн-оплате" not in rendered


def test_online_partner_points_do_not_apply_to_offline_payment(tmp_path) -> None:
    _stores, partners, brand_id = _repositories(tmp_path)
    partners.create_offer(_offer(brand_id), actor_id=1)

    resolved = resolve_store_matches(
        _catalog(tmp_path),
        partners,
        brand_id,
        "offline",
        "1234",
        on_date=date(2026, 8, 30),
    )
    vitamin = next(match for match in resolved if match.card.id == "vitamin_d")

    assert vitamin.gross_value == Decimal("3")
    assert format_moneyback(vitamin) == "3% баллами"
    assert vitamin.context_lines == ()


def test_actionable_partner_condition_stays_in_compact_context(tmp_path) -> None:
    _stores, partners, brand_id = _repositories(tmp_path)
    partners.create_offer(
        _offer(
            brand_id,
            conditions="Требуется подключить бесплатную функцию «Партнёрская сеть».",
        ),
        actor_id=1,
    )

    resolved = resolve_store_matches(
        _catalog(tmp_path), partners, brand_id, "online", "1234", on_date=date(2026, 8, 30)
    )
    vitamin = next(match for match in resolved if match.card.id == "vitamin_d")

    assert vitamin.context_lines == (
        "Требуется подключить бесплатную функцию «Партнёрская сеть» · "
        "сумма от 100 BYN · не больше 50 баллов за операцию · по 31.10.2026",
    )


def test_points_exclusion_falls_back_to_ordinary_money_with_reason(tmp_path) -> None:
    _stores, partners, brand_id = _repositories(tmp_path, "Unistore")
    partners.create_exclusion(
        PartnerExclusionInput(
            brand_id=brand_id,
            card_id="vitamin_d",
            reward_kind="points",
            channel="any",
            suppress_base=True,
            reason="С 15.11.2021 бонусные баллы не начисляются",
        ),
        actor_id=1,
    )

    resolved = resolve_store_matches(
        _catalog(tmp_path), partners, brand_id, "online", "5411", on_date=date(2026, 8, 30)
    )
    vitamin = next(match for match in resolved if match.card.id == "vitamin_d")

    assert format_moneyback(vitamin) == "1% деньгами"
    assert vitamin.gross_value == Decimal("1")
    assert vitamin.context_lines == (
        "Баллы Плюшек не начисляются: С 15.11.2021 бонусные баллы не начисляются",
    )


def test_specific_partner_exclusion_does_not_undo_broader_base_suppression(tmp_path) -> None:
    _stores, partners, brand_id = _repositories(tmp_path, "Unistore")
    partners.create_exclusion(
        PartnerExclusionInput(
            brand_id=brand_id,
            card_id="vitamin_d",
            reward_kind="points",
            channel="any",
            suppress_base=True,
            reason="Баллы программы не начисляются",
        ),
        actor_id=1,
    )
    partners.create_exclusion(
        PartnerExclusionInput(
            brand_id=brand_id,
            card_id="vitamin_d",
            reward_kind="points",
            channel="offline",
            mcc="5411",
            reason="Партнёрская акция не действует",
        ),
        actor_id=1,
    )

    resolved = resolve_store_matches(_catalog(tmp_path), partners, brand_id, "offline", "5411")
    vitamin = next(match for match in resolved if match.card.id == "vitamin_d")

    assert format_moneyback(vitamin) == "1% деньгами"
    assert vitamin.context_lines == ("Баллы Плюшек не начисляются: Партнёрская акция не действует",)


def test_total_points_replace_the_complete_ordinary_reward(tmp_path) -> None:
    _stores, partners, brand_id = _repositories(tmp_path, "Соседи")
    partners.create_offer(
        _offer(
            brand_id,
            card_id="cactus_mtbank",
            channel="any",
            mode="total",
            tiers=(PartnerTierInput(Decimal("3")),),
            starts_on=None,
            ends_on=None,
            conditions="",
        ),
        actor_id=1,
    )

    resolved = resolve_store_matches(_catalog(tmp_path), partners, brand_id, "offline", "5411")
    cactus = next(match for match in resolved if match.card.id == "cactus_mtbank")
    assert cactus.gross_value == Decimal("3")
    assert format_moneyback(cactus) == "3% баллами"


def test_excluded_total_offer_preserves_ordinary_same_kind_reward(tmp_path) -> None:
    _stores, partners, brand_id = _repositories(tmp_path, "Соседи")
    partners.create_exclusion(
        PartnerExclusionInput(
            brand_id=brand_id,
            card_id="cactus_mtbank",
            reward_kind="cash",
            channel="any",
            reason="Акция не действует для этой покупки",
        ),
        actor_id=1,
    )

    resolved = resolve_store_matches(_catalog(tmp_path), partners, brand_id, "offline", "5411")
    cactus = next(match for match in resolved if match.card.id == "cactus_mtbank")

    assert cactus.gross_value == Decimal("1")
    assert format_moneyback(cactus) == "1% деньгами"
    assert cactus.context_lines == (
        "Партнёрский манибэк не начисляется: Акция не действует для этой покупки",
    )


@pytest.mark.parametrize("total_kind,additional_kind", [("cash", "points"), ("points", "cash")])
def test_total_offer_wins_over_additional_offer_regardless_of_reward_kind_order(
    tmp_path, total_kind, additional_kind
) -> None:
    _stores, partners, brand_id = _repositories(tmp_path, "Mixed partner")
    partners.create_offer(
        _offer(
            brand_id,
            mode="total",
            reward_kind=total_kind,
            tiers=(PartnerTierInput(Decimal("7")),),
            starts_on=None,
            ends_on=None,
        ),
        actor_id=1,
    )
    partners.create_offer(
        _offer(
            brand_id,
            mode="additional",
            reward_kind=additional_kind,
            tiers=(PartnerTierInput(Decimal("5")),),
            starts_on=None,
            ends_on=None,
        ),
        actor_id=1,
    )

    resolved = resolve_store_matches(_catalog(tmp_path), partners, brand_id, "online", "1234")
    vitamin = next(match for match in resolved if match.card.id == "vitamin_d")

    assert vitamin.gross_value == Decimal("7")
    assert len(vitamin.components) == 1
    assert vitamin.components[0].kind == total_kind


def test_global_transport_mcc_exclusion_applies_to_arbitrary_brand(tmp_path) -> None:
    stores, partners, _brand_id = _repositories(tmp_path, "Городской автобус")
    result = stores.apply_change(
        "add_merchant", {"name": "Другой перевозчик", "channel": "offline", "mcc": "4111"}, 1
    )
    assert result.brand_id is not None
    partners.create_exclusion(
        PartnerExclusionInput(
            brand_id=None,
            card_id="vitamin_d",
            reward_kind="points",
            channel="any",
            mcc="4111",
            suppress_base=True,
            reason="Организации, осуществляющие пассажирские перевозки",
        ),
        actor_id=1,
    )

    resolved = resolve_store_matches(
        _catalog(tmp_path), partners, result.brand_id, "offline", "4111"
    )
    vitamin = next(match for match in resolved if match.card.id == "vitamin_d")
    assert format_moneyback(vitamin) == "1% деньгами"
    assert vitamin.context_lines == (
        "Баллы Плюшек не начисляются: Организации, осуществляющие пассажирские перевозки",
    )


def test_tiers_dates_crud_and_caller_transaction(tmp_path) -> None:
    stores, partners, brand_id = _repositories(tmp_path, "Zoobazar")
    payload = _offer(
        brand_id,
        channel="any",
        tiers=(
            PartnerTierInput(
                Decimal("1"), min_purchase=Decimal("20"), max_purchase=Decimal("69.99")
            ),
            PartnerTierInput(Decimal("5"), min_purchase=Decimal("150")),
        ),
    )
    with pytest.raises(RuntimeError), stores.transaction() as connection:
        partners.create_offer(payload, actor_id=1, connection=connection)
        raise RuntimeError("rollback")
    assert partners.list_offers(brand_id) == ()

    offer = partners.create_offer(payload, actor_id=1)
    low = partners.resolve(
        brand_id,
        "vitamin_d",
        "offline",
        "5411",
        amount=Decimal("30"),
        on_date=date(2026, 8, 30),
    )
    assert low is not None and low.value == Decimal("1")
    assert (
        partners.resolve(
            brand_id,
            "vitamin_d",
            "offline",
            "5411",
            on_date=date(2027, 1, 1),
        )
        is None
    )

    updated = partners.update_offer(
        offer.id,
        _offer(brand_id, conditions="Изменено вручную"),
        actor_id=2,
    )
    assert updated.conditions == "Изменено вручную"
    assert partners.delete_offer(offer.id, actor_id=2)
    assert partners.get_offer(offer.id) is None
    assert partners.restore_offer(offer.id, actor_id=2).archived is False
