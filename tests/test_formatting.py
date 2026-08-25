from __future__ import annotations

import json
from pathlib import Path

from mcc_bot.catalog import CardCatalog
from mcc_bot.descriptions import DescriptionCatalog
from mcc_bot.formatting import format_limits, format_matches, format_moneyback, split_message


def test_format_matches_renders_only_cards_and_rewards(catalog_path: Path, tmp_path: Path) -> None:
    matches = CardCatalog.from_file(catalog_path).lookup("5411")
    descriptions_path = tmp_path / "descriptions.json"
    descriptions_path.write_text('{"5411": "Продуктовые магазины"}', encoding="utf-8")
    descriptions = DescriptionCatalog.from_file(descriptions_path)

    rendered = format_matches("5411", matches, descriptions)

    assert rendered.startswith("🛒 MCC 5411 — Продуктовые магазины")
    assert "1. 🅱️ Beta Card — 5% (4,61% после налога)" in rendered
    assert "3. 🅰️ Alpha Card — 2,5% (2,44% после налога)" in rendered
    assert "Beta Bank" not in rendered
    assert "Alpha Bank" not in rendered
    assert "мин. платёж" not in rendered
    assert "макс. в месяц" not in rendered
    assert "after tax" not in rendered


def test_format_limits_renders_all_cards_and_program_terms(catalog_path: Path) -> None:
    rendered = format_limits(CardCatalog.from_file(catalog_path).cards)

    assert rendered.startswith("📊 Лимиты по картам\n\n")
    assert "1. 🅰️ Alpha Card — 💵 мин. платёж не указан · макс. в месяц не указан" in rendered
    assert "2. 🅱️ Beta Card — 💵 мин. платёж не указан · макс. в месяц не указан" in rendered
    assert "3. 🌀 Gamma Card — 💵 мин. платёж не указан · макс. в месяц не указан" in rendered
    assert "Alpha Bank" not in rendered


def test_format_matches_reports_no_cards_in_russian() -> None:
    assert format_matches("5411", ()) == (
        "🧾 MCC 5411 — описание не найдено\n\n❌ Доступных карт нет."
    )


def test_format_moneyback_tax_and_points_exemption(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "cards": [
                    {
                        "id": "cash",
                        "name": "Cash",
                        "emoji": "💳",
                        "reward_programs": [
                            {
                                "kind": "cash",
                                "tax_exempt": False,
                                "offers": [{"mcc": "5411", "value": 3}],
                            }
                        ],
                    },
                    {
                        "id": "points",
                        "name": "Points",
                        "emoji": "⭐",
                        "reward_programs": [
                            {
                                "kind": "points",
                                "tax_exempt": True,
                                "offers": [{"mcc": "5411", "value": 3}],
                            }
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    matches = CardCatalog.from_file(path).lookup("5411")
    by_kind = {match.components[0].kind: match for match in matches}
    assert format_moneyback(by_kind["points"]) == "3% баллами"
    assert format_moneyback(by_kind["cash"]) == "3% (2,87% после налога)"


def test_format_matches_does_not_render_internal_conditions(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "cards": [
                    {
                        "id": "card",
                        "name": "Card",
                        "emoji": "💳",
                        "condition": {"kind": "max_connected_categories", "count": 3},
                        "reward_programs": [
                            {
                                "kind": "points",
                                "tax_exempt": False,
                                "offers": [{"mcc": "5411", "value": 1}],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rendered = format_matches("5411", CardCatalog.from_file(path).lookup("5411"))
    assert "подключённых категорий" not in rendered
    assert "Note" not in rendered
    assert "1% баллами" in rendered


def test_split_message_prefers_line_boundaries() -> None:
    chunks = split_message("header\n" + "x" * 10 + "\nfooter", max_length=12)
    assert chunks == ("header", "xxxxxxxxxx", "footer")
