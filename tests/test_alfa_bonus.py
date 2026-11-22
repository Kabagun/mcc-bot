from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mcc_bot.alfa_bonus import ALFA_BONUS_CARD_ID, alfa_bonus_categories
from mcc_bot.bot import _lookup_and_reply, toggle_details
from mcc_bot.catalog import CardCatalog, PartnerPolicy
from mcc_bot.descriptions import DescriptionCatalog
from mcc_bot.partner_rewards import PartnerOfferInput, PartnerRepository, PartnerTierInput
from mcc_bot.resources import DEFAULT_CATALOG_PATH, PACKAGE_DATA_DIR
from mcc_bot.store_handlers import handle_store_callback, search_stores
from mcc_bot.stores import StoreRepository


@pytest.fixture(scope="module")
def catalog():
    return CardCatalog.from_file(DEFAULT_CATALOG_PATH)


def _names(catalog, mcc=None, *, merchant_names=()):
    return tuple(
        category.name
        for category in alfa_bonus_categories(catalog.cards, mcc, merchant_names=merchant_names)
    )


def test_published_categories_and_partner_policy(catalog):
    payload = json.loads((PACKAGE_DATA_DIR / "alfa_bonus_categories.json").read_text("utf-8"))
    rows = payload["categories"]
    codes = {mcc for row in rows for mcc in row.get("mccs", ())}
    assert len(rows) == 136
    assert sum("mccs" in row for row in rows) == 49
    assert sum("merchants" in row for row in rows) == 86
    assert len(codes) == 239
    assert codes <= DescriptionCatalog.from_file().labels.keys()
    assert {mcc for mcc in codes if len(alfa_bonus_categories(catalog.cards, mcc)) > 1} == {
        "5712",
        "7941",
    }

    alfa = next(card for card in catalog.cards if card.id == ALFA_BONUS_CARD_ID)
    assert alfa.partner_policy == PartnerPolicy("total", "points")
    assert alfa.partner_policy_explicit
    assert not alfa.offers
    assert all(program.default_value is None for program in alfa.reward_programs)


def test_matches_mcc_or_whole_merchant_names_and_applies_fallback_exclusions(catalog):
    assert _names(catalog, "5411") == ("Супермаркеты",)
    assert _names(catalog, "5499") == ("Другие покупки",)
    assert _names(catalog, "4215") == _names(catalog, "6535") == _names(catalog, "9701") == ()
    assert _names(catalog, "5712") == ("Дом, ремонт", "Мебель")
    assert _names(catalog, "5262", merchant_names=("Wildberries",)) == (
        "Wildberries",
        "Маркетплейсы",
    )
    assert _names(catalog, merchant_names=("Edostavka",)) == ("Евроопт, Edostavka",)
    assert _names(catalog, merchant_names=("Яндекс Go",)) == ("Яндекс Go",)
    assert _names(catalog, merchant_names=("Доставка Wildberries", "Другая пицца")) == ()


def test_raw_mcc_shows_one_two_line_position_and_expands_overlapping_category(catalog):
    message = SimpleNamespace(reply_text=AsyncMock(), is_accessible=True)
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"catalog": catalog, "descriptions": DescriptionCatalog.from_file()}
        )
    )
    asyncio.run(_lookup_and_reply(SimpleNamespace(effective_message=message), context, "7941"))
    compact = message.reply_text.await_args.args[0]
    assert compact.count("Альфа-Бонус") == 1
    assert compact.splitlines()[-2:] == [
        "🔴 <b>Альфа-Бонус</b> · выберите в INSNC",
        "   «Спа и фитнес» / «Аквапарки и бассейны»",
    ]
    assert "Другие покупки" not in compact
    data = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    callback = SimpleNamespace(
        data=data, answer=AsyncMock(), message=message, edit_message_text=AsyncMock()
    )

    asyncio.run(toggle_details(SimpleNamespace(callback_query=callback), context))

    expanded = callback.edit_message_text.await_args.args[0]
    assert expanded.count("Альфа-Бонус") == 1
    assert expanded.endswith("«Спа и фитнес» / «Аквапарки, бассейны, активный отдых»")


def test_store_uses_exact_category_until_applicable_partner_reward_is_known(catalog, tmp_path):
    stores = StoreRepository(tmp_path / "alfa-test.sqlite3")
    stores.initialize()
    partners = PartnerRepository(stores)
    partners.initialize()
    brand_id = stores.apply_change(
        "add_merchant", {"name": "Wildberries", "channel": "online", "mcc": "5262"}, 1
    ).brand_id
    message = SimpleNamespace(reply_text=AsyncMock(), is_accessible=True)
    query = SimpleNamespace(
        data=f"store:cards:{brand_id}:online:5262:0:0",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=message,
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_message=message,
        effective_chat=SimpleNamespace(type="private"),
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(
        user_data={},
        application=SimpleNamespace(
            bot_data={
                "catalog": catalog,
                "descriptions": DescriptionCatalog.from_file(),
                "stores": stores,
                "partners": partners,
            }
        ),
    )

    asyncio.run(handle_store_callback(update, context))
    categories = query.edit_message_text.await_args.args[0]
    assert categories.count("Альфа-Бонус") == 1
    assert "«Wildberries» / «Маркетплейсы»" in categories

    partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id=ALFA_BONUS_CARD_ID,
            channel="online",
            mode="total",
            reward_kind="points",
            tiers=(PartnerTierInput(Decimal("4")),),
        ),
        actor_id=1,
    )
    asyncio.run(handle_store_callback(update, context))
    partner = query.edit_message_text.await_args.args[0]
    assert partner.count("Альфа-Бонус") == 1
    assert "Альфа-Бонус</b> — 4% баллами" in partner
    assert "выберите в INSNC" not in partner
    assert not any(match.card.id == ALFA_BONUS_CARD_ID for match in catalog.lookup("5262"))

    asyncio.run(search_stores(update, context, "Додо Пицца"))
    no_facts = message.reply_text.await_args.args[0]
    assert "Альфа-Бонус" in no_facts and "«Додо Пицца»" in no_facts
