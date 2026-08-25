from __future__ import annotations

from mcc_bot.cli import main
from mcc_bot.config import DEFAULT_CATALOG_PATH, DEFAULT_DESCRIPTIONS_PATH

DATA_PATH = DEFAULT_CATALOG_PATH
DESCRIPTION_PATH = DEFAULT_DESCRIPTIONS_PATH


def test_cli_prints_russian_unicode_card_names(capsys) -> None:
    assert (
        main(
            [
                "--catalog",
                str(DATA_PATH),
                "--descriptions",
                str(DESCRIPTION_PATH),
                "--mcc",
                "5411",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "🛒 MCC 5411 — Продуктовые магазины" in output
    assert "🟢💳 Витамин Д" in output
    assert "2,5% (2,44% после налога)" in output


def test_cli_json_contains_all_components(capsys) -> None:
    assert (
        main(
            [
                "--catalog",
                str(DATA_PATH),
                "--descriptions",
                str(DESCRIPTION_PATH),
                "--mcc",
                "5411",
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"gross_percent": "1"' in output
    assert '"tax_exempt": true' in output


def test_cli_uses_bundled_resources_outside_checkout(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["--mcc", "5411"]) == 0
    assert "🛒 MCC 5411 — Продуктовые магазины" in capsys.readouterr().out
