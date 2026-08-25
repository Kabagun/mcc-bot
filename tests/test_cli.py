from __future__ import annotations

from pathlib import Path

from mcc_bot.cli import main

DATA_PATH = Path(__file__).parents[1] / "data" / "cards.json"


def test_cli_prints_unicode_card_names(capsys) -> None:
    assert main(["--catalog", str(DATA_PATH), "--mcc", "5411"]) == 0

    output = capsys.readouterr().out
    assert "Шоппер МТБанк" in output
    assert "2.5% (2.44% after tax)" in output
