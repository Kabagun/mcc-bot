from __future__ import annotations

from pathlib import Path

from mcc_bot.catalog import CardCatalog
from mcc_bot.formatting import format_matches, format_moneyback, split_message


def test_format_matches_is_sorted_and_includes_issuer(catalog_path: Path) -> None:
    matches = CardCatalog.from_file(catalog_path).lookup("5411")

    assert format_matches("5411", matches) == (
        "Cards for MCC 5411 (highest moneyback first):\n"
        "1. Beta Card — Beta Bank: 5% (4.61% after tax)\n"
        "2. Gamma Card: 5% (4.61% after tax)\n"
        "3. Alpha Card — Alpha Bank: 2.5% (2.44% after tax)"
    )


def test_format_matches_reports_no_cards() -> None:
    assert format_matches("5411", ()) == "No cards found for MCC 5411."


def test_format_moneyback_supports_currency(catalog_path: Path, tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        '{"version": 1, "cards": [{"id": "card", "name": "Card", "offers": '
        '[{"mcc": "5411", "moneyback": 2.50, "unit": "currency", "currency": "BYN"}]}]}',
        encoding="utf-8",
    )

    match = CardCatalog.from_file(path).lookup("5411")[0]
    assert format_moneyback(match) == "2.5 BYN"


def test_format_moneyback_shows_taxed_net_percentage(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        '{"version": 1, "cards": [{"id": "card", "name": "Card", "offers": '
        '[{"mcc": "5411", "moneyback": 3, "unit": "percent"}]}]}',
        encoding="utf-8",
    )

    match = CardCatalog.from_file(path).lookup("5411")[0]
    assert format_moneyback(match) == "3% (2.87% after tax)"


def test_format_moneyback_does_not_tax_two_percent_or_less(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        '{"version": 1, "cards": [{"id": "card", "name": "Card", "offers": '
        '[{"mcc": "5411", "moneyback": 2, "unit": "percent"}]}]}',
        encoding="utf-8",
    )

    match = CardCatalog.from_file(path).lookup("5411")[0]
    assert format_moneyback(match) == "2%"


def test_format_matches_includes_card_and_offer_notes(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        '{"version": 1, "cards": [{"id": "card", "name": "Card", '
        '"notes": "Only one connected group", "offers": [{"mcc": "5411", '
        '"moneyback": 3, "notes": "Selected group"}]}]}',
        encoding="utf-8",
    )

    matches = CardCatalog.from_file(path).lookup("5411")
    assert "Note: Only one connected group" in format_matches("5411", matches)
    assert "Offer note: Selected group" in format_matches("5411", matches)


def test_split_message_prefers_line_boundaries() -> None:
    chunks = split_message("header\n" + "x" * 10 + "\nfooter", max_length=12)

    assert chunks == ("header", "xxxxxxxxxx", "footer")
