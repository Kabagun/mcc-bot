from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import BotCommand
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError
from telegram.ext import CallbackQueryHandler, CommandHandler

from mcc_bot.bot import (
    _configure_bot_commands,
    _lookup_and_reply,
    build_application,
    lookup_mcc_callback,
    lookup_text,
    remember_chat,
    start,
    toggle_details,
    unknown_command,
)
from mcc_bot.catalog import CardCatalog
from mcc_bot.community import CommunityService
from mcc_bot.config import BotSettings
from mcc_bot.descriptions import DescriptionCatalog
from mcc_bot.formatting import format_match_pages, format_matches
from mcc_bot.stores import StoreRepository
from mcc_bot.users import UserRegistry


@pytest.fixture(autouse=True)
def isolate_community_dispatch(monkeypatch):
    """Keep existing MCC unit tests independent of the separately tested form UI."""

    handler = AsyncMock(return_value=False)
    monkeypatch.setattr("mcc_bot.bot.handle_community_text", handler)
    return handler


def _context(catalog: CardCatalog) -> SimpleNamespace:
    return SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "catalog": catalog,
                "descriptions": DescriptionCatalog(
                    labels={
                        "5411": "Продуктовые магазины",
                        "5812": "Места общественного питания",
                    }
                ),
            }
        ),
        args=[],
        user_data={},
    )


def test_start_opens_role_aware_menu(catalog_path, monkeypatch) -> None:
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message)

    menu = AsyncMock()
    monkeypatch.setattr("mcc_bot.bot.show_menu", menu)
    context = _context(CardCatalog.from_file(catalog_path))
    asyncio.run(start(update, context))
    menu.assert_awaited_once_with(update, context)


def test_bare_mcc_replies_with_sorted_russian_results(catalog_path) -> None:
    message = SimpleNamespace(text="5411", reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message)
    context = _context(CardCatalog.from_file(catalog_path))
    asyncio.run(lookup_text(update, context))

    result = message.reply_text.await_args.args[0]
    assert result.index("Beta Card") < result.index("Alpha Card")
    assert "Продуктовые магазины" in result


@pytest.mark.parametrize("text", ["MCC 5812", "MCC:5411", "Евроопт", "А-100", "21 век"])  # noqa: RUF001
def test_non_numeric_text_routes_to_store_search(catalog_path, monkeypatch, text) -> None:
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message)
    search = AsyncMock()
    monkeypatch.setattr("mcc_bot.bot.search_stores", search)
    context = _context(CardCatalog.from_file(catalog_path))
    asyncio.run(lookup_text(update, context))
    search.assert_awaited_once_with(update, context, text)
    message.reply_text.assert_not_awaited()


def _numeric_brand(brand_id=77, *, name="7745", aliases=()):
    return SimpleNamespace(id=brand_id, name=name, aliases=tuple(aliases))


def _with_store_search(context, *, matches=(), suggestions=()):
    search = Mock(
        return_value=SimpleNamespace(
            matches=tuple(matches),
            suggestions=tuple(suggestions),
        )
    )
    context.application.bot_data["stores"] = SimpleNamespace(search=search)
    return search


def test_only_exact_numeric_brand_opens_brand_search(catalog_path, monkeypatch) -> None:
    context = _context(CardCatalog.from_file(catalog_path))
    context.application.bot_data["descriptions"] = DescriptionCatalog(labels={})
    repository_search = _with_store_search(context, matches=(_numeric_brand(),))
    store_search = AsyncMock()
    monkeypatch.setattr("mcc_bot.bot.search_stores", store_search)
    message = SimpleNamespace(text="7745", reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message)

    asyncio.run(lookup_text(update, context))

    repository_search.assert_called_once_with("7745", limit=100)
    store_search.assert_awaited_once_with(update, context, "7745")
    message.reply_text.assert_not_awaited()


def test_exact_numeric_alias_counts_as_brand(catalog_path, monkeypatch) -> None:
    context = _context(CardCatalog.from_file(catalog_path))
    context.application.bot_data["descriptions"] = DescriptionCatalog(labels={})
    _with_store_search(
        context,
        matches=(_numeric_brand(name="Official name", aliases=("7745",)),),
    )
    store_search = AsyncMock()
    monkeypatch.setattr("mcc_bot.bot.search_stores", store_search)
    update = SimpleNamespace(effective_message=SimpleNamespace(text="7745", reply_text=AsyncMock()))

    asyncio.run(lookup_text(update, context))

    store_search.assert_awaited_once_with(update, context, "7745")


def test_only_known_mcc_bypasses_fuzzy_numeric_brand(catalog_path, monkeypatch) -> None:
    context = _context(CardCatalog.from_file(catalog_path))
    context.application.bot_data["descriptions"] = DescriptionCatalog(
        labels={"7745": "Известная категория"}
    )
    fuzzy = _numeric_brand(name="77450")
    _with_store_search(context, matches=(fuzzy,), suggestions=(fuzzy,))
    lookup = AsyncMock()
    store_search = AsyncMock()
    monkeypatch.setattr("mcc_bot.bot._lookup_and_reply", lookup)
    monkeypatch.setattr("mcc_bot.bot.search_stores", store_search)
    update = SimpleNamespace(effective_message=SimpleNamespace(text="7745", reply_text=AsyncMock()))

    asyncio.run(lookup_text(update, context))

    lookup.assert_awaited_once_with(update, context, "7745")
    store_search.assert_not_awaited()


def test_numeric_brand_and_mcc_offer_explicit_choice(catalog_path) -> None:
    context = _context(CardCatalog.from_file(catalog_path))
    context.application.bot_data["descriptions"] = DescriptionCatalog(
        labels={"7745": "Известная категория"}
    )
    _with_store_search(context, matches=(_numeric_brand(),))
    message = SimpleNamespace(text="7745", reply_text=AsyncMock())

    asyncio.run(lookup_text(SimpleNamespace(effective_message=message), context))

    message.reply_text.assert_awaited_once()
    rows = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert [row[0].text for row in rows] == ["🏪 Бренд «7745»", "🧾 MCC 7745"]
    assert rows[0][0].callback_data == "store:show:77:0"
    assert rows[1][0].callback_data == "mcc_lookup:7745"


def test_unknown_numeric_input_explains_mcc_and_offers_prefilled_brand(catalog_path) -> None:
    context = _context(CardCatalog.from_file(catalog_path))
    context.application.bot_data["descriptions"] = DescriptionCatalog(labels={})
    _with_store_search(context)
    message = SimpleNamespace(text="1233", reply_text=AsyncMock())

    asyncio.run(lookup_text(SimpleNamespace(effective_message=message), context))

    message.reply_text.assert_awaited_once()
    text = message.reply_text.await_args.args[0]
    assert text.splitlines()[0] == "MCC 1233 не найден в справочнике. Проверьте код."
    assert "Если «1233» — название магазина, его можно добавить." in text  # noqa: RUF001
    button = message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "➕ Добавить бренд «1233»"  # noqa: RUF001
    _, _, _, token = button.callback_data.split(":")
    assert context.user_data["store_searches"][token] == "1233"


def test_unknown_mcc_lookup_never_calculates_cards() -> None:
    catalog = SimpleNamespace(lookup=Mock(side_effect=AssertionError("must not calculate")))
    context = _context(catalog)
    context.application.bot_data["descriptions"] = DescriptionCatalog(labels={})
    message = SimpleNamespace(reply_text=AsyncMock())

    asyncio.run(
        _lookup_and_reply(
            SimpleNamespace(effective_message=message),
            context,
            "1233",
        )
    )

    catalog.lookup.assert_not_called()
    message.reply_text.assert_awaited_once_with("MCC 1233 не найден в справочнике. Проверьте код.")


def test_explicit_mcc_choice_uses_lookup_without_brand_creation() -> None:
    catalog = SimpleNamespace(lookup=Mock(side_effect=AssertionError("must not calculate")))
    context = _context(catalog)
    context.application.bot_data["descriptions"] = DescriptionCatalog(labels={})
    message = SimpleNamespace(is_accessible=True, reply_text=AsyncMock())
    query = SimpleNamespace(
        data="mcc_lookup:1233",
        message=message,
        answer=AsyncMock(),
    )

    asyncio.run(
        lookup_mcc_callback(
            SimpleNamespace(callback_query=query, effective_message=message),
            context,
        )
    )

    query.answer.assert_awaited_once()
    catalog.lookup.assert_not_called()
    message.reply_text.assert_awaited_once_with("MCC 1233 не найден в справочнике. Проверьте код.")
    assert "store_searches" not in context.user_data


def test_active_form_consumes_numeric_text_before_lookup(catalog_path, isolate_community_dispatch):
    isolate_community_dispatch.return_value = True
    message = SimpleNamespace(text="5411", reply_text=AsyncMock())
    asyncio.run(
        lookup_text(
            SimpleNamespace(effective_message=message),
            _context(CardCatalog.from_file(catalog_path)),
        )
    )
    message.reply_text.assert_not_awaited()


@pytest.mark.parametrize("text", ["/mcc 5411", "/help", "/unknown"])
def test_removed_commands_only_explain_supported_input(catalog_path, text):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    asyncio.run(
        unknown_command(
            SimpleNamespace(effective_message=message),
            _context(CardCatalog.from_file(catalog_path)),
        )
    )
    result = message.reply_text.await_args.args[0]
    assert "/start" in result and "Информация по картам" in result
    assert "Alpha Card" not in result and "Beta Card" not in result


def test_command_menu_lists_only_start_with_russian_description() -> None:
    set_commands = AsyncMock()
    set_name = AsyncMock()
    set_short_description = AsyncMock()
    application = SimpleNamespace(
        bot=SimpleNamespace(
            set_my_commands=set_commands,
            set_my_name=set_name,
            set_my_short_description=set_short_description,
        )
    )

    asyncio.run(_configure_bot_commands(application))

    set_commands.assert_awaited_once_with(
        [BotCommand(command="start", description="Начало и меню")]
    )
    set_name.assert_awaited_once_with("Какой картой?")
    set_short_description.assert_awaited_once_with("Подскажет лучшую карту по магазину или MCC.")


def test_remember_chat_persists_effective_chat_id(catalog_path, tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    context = _context(CardCatalog.from_file(catalog_path))
    context.application.bot_data["user_registry"] = registry
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=12345))

    asyncio.run(remember_chat(update, context))

    assert registry.chat_ids() == (12345,)


def test_remember_chat_refreshes_only_known_role_identity(catalog_path, tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    community = CommunityService(StoreRepository(tmp_path / "stores.sqlite3"), owner_id=1)
    community.initialize()
    community.request_role(10, "old_name", "Old")
    context = _context(CardCatalog.from_file(catalog_path))
    context.application.bot_data.update({"user_registry": registry, "community": community})
    known = SimpleNamespace(
        effective_chat=SimpleNamespace(id=10),
        effective_user=SimpleNamespace(
            id=10, username="new_name", first_name="New", last_name="Helper"
        ),
    )
    unknown = SimpleNamespace(
        effective_chat=SimpleNamespace(id=20),
        effective_user=SimpleNamespace(
            id=20, username="ordinary", first_name="Ordinary", last_name=None
        ),
    )

    asyncio.run(remember_chat(known, context))
    asyncio.run(remember_chat(unknown, context))

    candidate = community.role_candidates(1)[0]
    assert (candidate["username"], candidate["first_name"]) == ("new_name", "New")
    with community.stores.connection() as connection:
        assert (
            connection.execute("SELECT 1 FROM community_role_profiles WHERE user_id=20").fetchone()
            is None
        )


def _callback(data: object, *, accessible: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(is_accessible=accessible, reply_text=AsyncMock()),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


def _button(call):
    return call.kwargs["reply_markup"].inline_keyboard[0][0]


@pytest.mark.parametrize("raw_mcc", ["5411", " 5411 "])
def test_bare_lookup_attaches_details_button_with_normalized_mcc(catalog_path, raw_mcc):
    catalog = CardCatalog.from_file(catalog_path)
    context = _context(catalog)
    context.args = raw_mcc.split()
    message = SimpleNamespace(text=raw_mcc, reply_text=AsyncMock())

    asyncio.run(lookup_text(SimpleNamespace(effective_message=message), context))

    message.reply_text.assert_awaited_once()
    button = _button(message.reply_text.await_args)
    assert button.text == "🏦 Банки и минимальный платёж"
    assert button.callback_data == "mcc_details:5411:0:1"
    assert len(button.callback_data.encode()) <= 64
    assert message.reply_text.await_args.kwargs["parse_mode"] == ParseMode.HTML
    assert message.reply_text.await_args.args[0] == format_matches(
        "5411", catalog.lookup("5411"), context.application.bot_data["descriptions"], html=True
    )


@pytest.mark.parametrize("raw_mcc", ["123", "12345", "9999"])
def test_invalid_or_empty_lookups_have_no_details_button(catalog_path, raw_mcc) -> None:
    message = SimpleNamespace(text=raw_mcc, reply_text=AsyncMock())

    asyncio.run(
        lookup_text(
            SimpleNamespace(effective_message=message),
            _context(CardCatalog.from_file(catalog_path)),
        )
    )

    message.reply_text.assert_awaited_once()
    assert "parse_mode" not in message.reply_text.await_args.kwargs
    if raw_mcc == "9999":
        assert "reply_markup" in message.reply_text.await_args.kwargs
        assert (
            message.reply_text.await_args.args[0].splitlines()[0]
            == "MCC 9999 не найден в справочнике. Проверьте код."
        )
    else:
        assert "reply_markup" not in message.reply_text.await_args.kwargs


def test_details_roundtrip_edits_the_same_message_and_restores_exact_compact_text(catalog_path):
    context = _context(CardCatalog.from_file(catalog_path))
    incoming = SimpleNamespace(text="5411", reply_text=AsyncMock())
    asyncio.run(lookup_text(SimpleNamespace(effective_message=incoming), context))
    compact = incoming.reply_text.await_args.args[0]
    assert "<b>Beta Card</b> — 5% (4,61%)" in compact
    assert "<b>Alpha Card</b> — 2,5% (2,44%)" in compact
    query = _callback(_button(incoming.reply_text.await_args).callback_data)

    asyncio.run(toggle_details(SimpleNamespace(callback_query=query), context))

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    assert "<i>Beta Bank</i>" in query.edit_message_text.await_args.args[0]
    assert "<b>Beta Card</b>" in query.edit_message_text.await_args.args[0]
    assert query.edit_message_text.await_args.kwargs["parse_mode"] == ParseMode.HTML
    button = _button(query.edit_message_text.await_args)
    assert button.text == "Скрыть подробности"
    assert button.callback_data == "mcc_details:5411:0:0"
    query.data = button.callback_data

    asyncio.run(toggle_details(SimpleNamespace(callback_query=query), context))

    assert query.edit_message_text.await_count == 2
    assert query.edit_message_text.await_args.args[0] == compact
    assert query.edit_message_text.await_args.kwargs["parse_mode"] == ParseMode.HTML
    assert _button(query.edit_message_text.await_args).text == "🏦 Банки и минимальный платёж"
    query.message.reply_text.assert_not_awaited()
    incoming.reply_text.assert_awaited_once()
    assert set(context.application.bot_data) == {"catalog", "descriptions"}


def test_html_catalog_text_remains_literal_through_lookup_and_details(catalog_path) -> None:
    base = CardCatalog.from_file(catalog_path).cards[0]
    card = replace(base, name="A & <B>", issuer="Bank & <issuer>")
    context = _context(CardCatalog((card,)))
    context.application.bot_data["descriptions"] = DescriptionCatalog(
        labels={"5411": "Food & <shops>"}
    )
    message = SimpleNamespace(text="5411", reply_text=AsyncMock())

    asyncio.run(lookup_text(SimpleNamespace(effective_message=message), context))

    compact = message.reply_text.await_args.args[0]
    assert "<b>A &amp; &lt;B&gt;</b>" in compact
    assert "Food &amp; &lt;shops&gt;" in compact
    assert message.reply_text.await_args.kwargs["parse_mode"] == ParseMode.HTML
    query = _callback(_button(message.reply_text.await_args).callback_data)
    asyncio.run(toggle_details(SimpleNamespace(callback_query=query), context))
    expanded = query.edit_message_text.await_args.args[0]
    assert "<i>Bank &amp; &lt;issuer&gt;</i>" in expanded
    assert "<issuer>" not in expanded
    assert query.edit_message_text.await_args.kwargs["parse_mode"] == ParseMode.HTML


def test_details_state_is_independent_for_messages_even_with_same_mcc(catalog_path) -> None:
    context = _context(CardCatalog.from_file(catalog_path))
    first, second, third = (
        _callback("mcc_details:5411:0:1"),
        _callback("mcc_details:5812:0:1"),
        _callback("mcc_details:5411:0:1"),
    )
    for query in (first, second, third):
        asyncio.run(toggle_details(SimpleNamespace(callback_query=query), context))

    first_expanded = first.edit_message_text.await_args.args[0]
    assert third.edit_message_text.await_args.args[0] == first_expanded
    second_expanded = second.edit_message_text.await_args.args[0]
    assert "MCC 5812" in second_expanded
    assert "Alpha Bank" in second_expanded
    assert "Beta Bank" not in second_expanded
    first.data = "mcc_details:5411:0:0"
    asyncio.run(toggle_details(SimpleNamespace(callback_query=first), context))
    assert "Beta Bank" not in first.edit_message_text.await_args.args[0]
    second.edit_message_text.assert_awaited_once()
    third.edit_message_text.assert_awaited_once()


@pytest.mark.parametrize(
    "data",
    [
        None,
        object(),
        "",
        "other:5411:0:1",
        "mcc_details:541:0:1",
        "mcc_details:５４１１:0:1",  # noqa: RUF001
        "mcc_details:5411:-1:1",
        "mcc_details:5411:00:1",
        "mcc_details:5411:1.0:1",
        "mcc_details:5411:1000000:1",
        "mcc_details:5411:0:2",
        "mcc_details:5411:0:1\n",
        "mcc_details:5411:0:1:extra",
        "mcc_details:5411:999999:1",
        "mcc_details:5411:1:1",
        "mcc_details:9999:0:1",
    ],
)
def test_malformed_or_out_of_range_callbacks_are_acknowledged_without_edits(catalog_path, data):
    query = _callback(data)

    asyncio.run(
        toggle_details(
            SimpleNamespace(callback_query=query), _context(CardCatalog.from_file(catalog_path))
        )
    )

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_not_awaited()
    query.message.reply_text.assert_not_awaited()


def test_large_forged_page_is_rejected_before_catalog_lookup() -> None:
    class NoLookupCatalog:
        cards = (object(),)

        def lookup(self, _mcc):
            pytest.fail("Out-of-range callback must not trigger lookup")

    query = _callback("mcc_details:5411:999999:1")
    asyncio.run(toggle_details(SimpleNamespace(callback_query=query), _context(NoLookupCatalog())))

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_not_awaited()


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_inaccessible_callback_message_is_acknowledged(catalog_path, missing):
    query = _callback("mcc_details:5411:0:1", accessible=False)
    if missing:
        query.message = None

    asyncio.run(
        toggle_details(
            SimpleNamespace(callback_query=query), _context(CardCatalog.from_file(catalog_path))
        )
    )

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        BadRequest("Message is not modified: specified new message content is identical"),
        BadRequest("Message to edit not found"),
        Forbidden("Forbidden"),
        NetworkError("Connection failed"),
        TypeError("Cannot edit an inaccessible message"),
    ],
)
def test_repeated_clicks_and_edit_failures_do_not_send_new_messages(catalog_path, error):
    query = _callback("mcc_details:5411:0:1")
    query.edit_message_text.side_effect = error

    asyncio.run(
        toggle_details(
            SimpleNamespace(callback_query=query), _context(CardCatalog.from_file(catalog_path))
        )
    )

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    query.message.reply_text.assert_not_awaited()


def test_expired_callback_acknowledgement_is_handled(catalog_path) -> None:
    query = _callback("mcc_details:5411:0:1")
    query.answer.side_effect = BadRequest("Query is too old")

    asyncio.run(
        toggle_details(
            SimpleNamespace(callback_query=query), _context(CardCatalog.from_file(catalog_path))
        )
    )

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_not_awaited()


def test_missing_callback_is_ignored(catalog_path) -> None:
    asyncio.run(
        toggle_details(
            SimpleNamespace(callback_query=None), _context(CardCatalog.from_file(catalog_path))
        )
    )


def test_large_catalog_replies_in_stable_pages_and_each_toggles_in_place(catalog_path) -> None:
    base = CardCatalog.from_file(catalog_path).cards[0]
    catalog = CardCatalog(
        tuple(replace(base, id=f"card-{i}", name=f"Card {i:03d}") for i in range(100))
    )
    context = _context(catalog)
    message = SimpleNamespace(text="5411", reply_text=AsyncMock())

    asyncio.run(lookup_text(SimpleNamespace(effective_message=message), context))

    pages = format_match_pages(
        "5411", catalog.lookup("5411"), context.application.bot_data["descriptions"], html=True
    )
    assert len(pages) > 1
    assert message.reply_text.await_count == len(pages)
    for index, call in enumerate(message.reply_text.await_args_list):
        assert call.args[0] == pages[index].compact
        assert call.kwargs["parse_mode"] == ParseMode.HTML
        assert _button(call).callback_data == f"mcc_details:5411:{index}:1"
        query = _callback(_button(call).callback_data)
        asyncio.run(toggle_details(SimpleNamespace(callback_query=query), context))
        assert query.edit_message_text.await_args.args[0] == pages[index].expanded
        assert query.edit_message_text.await_args.kwargs["parse_mode"] == ParseMode.HTML
        query.data = _button(query.edit_message_text.await_args).callback_data
        asyncio.run(toggle_details(SimpleNamespace(callback_query=query), context))
        assert query.edit_message_text.await_args.args[0] == call.args[0]
        assert query.edit_message_text.await_args.kwargs["parse_mode"] == ParseMode.HTML
        query.message.reply_text.assert_not_awaited()
    assert message.reply_text.await_count == len(pages)


def test_oversized_card_gives_explicit_bounded_reply_without_losing_cards_silently(catalog_path):
    base = CardCatalog.from_file(catalog_path).cards[0]
    context = _context(CardCatalog((replace(base, name="x" * 5000),)))
    message = SimpleNamespace(text="5411", reply_text=AsyncMock())

    asyncio.run(lookup_text(SimpleNamespace(effective_message=message), context))

    message.reply_text.assert_awaited_once()
    assert "слишком длинные" in message.reply_text.await_args.args[0]
    assert "Информация по картам" in message.reply_text.await_args.args[0]
    assert "reply_markup" not in message.reply_text.await_args.kwargs

    query = _callback("mcc_details:5411:0:1")
    asyncio.run(toggle_details(SimpleNamespace(callback_query=query), context))
    assert query.edit_message_text.await_args.args[0] == message.reply_text.await_args.args[0]
    assert query.edit_message_text.await_args.kwargs["reply_markup"] is None


def test_application_registers_details_callback_handler(catalog_path, tmp_path) -> None:
    settings = BotSettings(
        token="123456:ABC",
        catalog_path=catalog_path,
        descriptions_path=tmp_path / "descriptions.json",
        user_registry_path=tmp_path / "users.sqlite3",
        stores_path=tmp_path / "stores.sqlite3",
        owner_telegram_id=12345,
    )
    settings.descriptions_path.write_text('{"5411": "Продуктовые магазины"}', encoding="utf-8")

    application = build_application(settings)

    handlers = [handler for group in application.handlers.values() for handler in group]
    assert any(
        isinstance(handler, CallbackQueryHandler) and handler.callback is toggle_details
        for handler in handlers
    )
    commands = {
        command
        for handler in handlers
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }
    assert commands == {"start"}
    patterns = {
        handler.pattern.pattern
        for handler in handlers
        if isinstance(handler, CallbackQueryHandler) and handler.pattern
    }
    assert patterns == {"^mcc_details:", "^mcc_lookup:", "^store:", "^community:"}
