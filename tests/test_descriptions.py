from __future__ import annotations

from pathlib import Path

import pytest

from mcc_bot.catalog import CatalogError
from mcc_bot.descriptions import DescriptionCatalog


def test_description_catalog_normalizes_keys_and_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "descriptions.json"
    path.write_text(
        '{"0742": "  Ветеринарные\\nуслуги  "}',  # noqa: RUF001
        encoding="utf-8",
    )
    descriptions = DescriptionCatalog.from_file(path)
    assert descriptions.get("MCC:0742") == "Ветеринарные услуги"
    assert descriptions.get("5411") == "описание не найдено"


def test_description_catalog_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "descriptions.json"
    path.write_bytes(b'{"0742": "\xff"}')
    with pytest.raises(CatalogError, match="UTF-8"):
        DescriptionCatalog.from_file(path)
