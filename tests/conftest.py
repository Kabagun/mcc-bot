from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "cards": [
                    {
                        "id": "alpha",
                        "name": "Alpha Card",
                        "issuer": "Alpha Bank",
                        "offers": [
                            {"mcc": "5411", "moneyback": 2.5, "unit": "percent"},
                            {"mcc": "5812", "moneyback": 1, "unit": "percent"},
                        ],
                    },
                    {
                        "id": "beta",
                        "name": "Beta Card",
                        "issuer": "Beta Bank",
                        "offers": [{"mcc": "5411", "moneyback": 5, "unit": "percent"}],
                    },
                    {
                        "id": "gamma",
                        "name": "Gamma Card",
                        "offers": [{"mcc": "5411", "moneyback": 5, "unit": "percent"}],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path
