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
                "version": 2,
                "cards": [
                    {
                        "id": "alpha",
                        "name": "Alpha Card",
                        "issuer": "Alpha Bank",
                        "emoji": "🅰️",
                        "reward_programs": [
                            {
                                "kind": "cash",
                                "tax_exempt": False,
                                "offers": [
                                    {"mcc": "5411", "value": 2.5},
                                    {"mcc": "5812", "value": 1},
                                ],
                            }
                        ],
                    },
                    {
                        "id": "beta",
                        "name": "Beta Card",
                        "issuer": "Beta Bank",
                        "emoji": "🅱️",
                        "reward_programs": [
                            {
                                "kind": "cash",
                                "tax_exempt": False,
                                "offers": [{"mcc": "5411", "value": 5}],
                            }
                        ],
                    },
                    {
                        "id": "gamma",
                        "name": "Gamma Card",
                        "emoji": "🌀",
                        "reward_programs": [
                            {
                                "kind": "cash",
                                "tax_exempt": False,
                                "offers": [{"mcc": "5411", "value": 5}],
                            }
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path
