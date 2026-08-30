from __future__ import annotations

# Russian UI copy and ordinary Unicode buttons are intentional.
# ruff: noqa: RUF001
import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcc_bot.catalog import CardCatalog
from mcc_bot.descriptions import DescriptionCatalog
from mcc_bot.partner_rewards import (
    PartnerOfferInput,
    PartnerRepository,
    PartnerTierInput,
)
from mcc_bot.store_handlers import handle_store_callback, search_stores
from mcc_bot.stores import StoreRepository


def _context(tmp_path, catalog_path, *, brand_name: str, mcc: str | None):
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    payload = {"name": brand_name, "channel": "online"}
    if mcc is not None:
        payload["mcc"] = mcc
    result = stores.apply_change("add_merchant", payload, 1)
    assert result.brand_id is not None
    partners = PartnerRepository(stores)
    partners.initialize()
    message = SimpleNamespace(text=brand_name, reply_text=AsyncMock(), is_accessible=True)
    callback = SimpleNamespace(
        data=None, answer=AsyncMock(), edit_message_text=AsyncMock(), message=message
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=10),
        effective_chat=SimpleNamespace(type="private"),
        callback_query=callback,
    )
    context = SimpleNamespace(
        args=None,
        user_data={},
        application=SimpleNamespace(
            bot_data={
                "stores": stores,
                "partners": partners,
                "catalog": CardCatalog.from_file(catalog_path),
                "descriptions": DescriptionCatalog({"5411": "Продуктовые магазины"}),
            }
        ),
    )
    return stores, partners, result.brand_id, update, context


def _offer(brand_id: int, *, channel: str = "any", value: str = "3") -> PartnerOfferInput:
    return PartnerOfferInput(
        brand_id=brand_id,
        card_id="alpha",
        channel=channel,
        mode="total",
        reward_kind="cash",
        tiers=(PartnerTierInput(Decimal(value)),),
        conditions="Только у партнёра",
    )


def test_partner_only_store_is_searchable_and_warns_without_card_comparison(
    tmp_path, catalog_path
) -> None:
    _stores, partners, brand_id, update, context = _context(
        tmp_path, catalog_path, brand_name="Partner Only", mcc=None
    )
    partners.create_offer(_offer(brand_id), actor_id=1)

    asyncio.run(search_stores(update, context, "Partner Only"))
    text = update.effective_message.reply_text.await_args.args[0]

    assert "🎁 Партнёрские предложения" in text
    assert "Alpha Card — 3% деньгами · офлайн и онлайн" in text
    assert "Только у партнёра" in text
    assert "MCC магазина пока не подтверждён — обычные карты не сравниваются" in text


def test_store_overview_keeps_trailing_zeroes_in_whole_percent(tmp_path, catalog_path) -> None:
    _stores, partners, brand_id, update, context = _context(
        tmp_path, catalog_path, brand_name="Twenty Percent", mcc=None
    )
    partners.create_offer(_offer(brand_id, value="20"), actor_id=1)

    asyncio.run(search_stores(update, context, "Twenty Percent"))
    text = update.effective_message.reply_text.await_args.args[0]

    assert "Alpha Card — 20% деньгами" in text
    assert "Alpha Card — 2% деньгами" not in text


def test_store_card_callback_is_partner_aware_but_raw_mcc_is_not(tmp_path, catalog_path) -> None:
    _stores, partners, brand_id, update, context = _context(
        tmp_path, catalog_path, brand_name="Partner Market", mcc="5411"
    )
    partners.create_offer(_offer(brand_id, channel="online"), actor_id=1)
    catalog = context.application.bot_data["catalog"]
    assert catalog.lookup("5411")[2].gross_value == Decimal("2.5")
    assert catalog.lookup("5411")[2].context_lines == ()

    update.callback_query.data = f"store:cards:{brand_id}:online:5411:0:0"
    asyncio.run(handle_store_callback(update, context))
    text = update.callback_query.edit_message_text.await_args.args[0]

    assert "Alpha Card</b> — 3% деньгами" in text
    assert "Партнёрское предложение · Только у партнёра" in text
