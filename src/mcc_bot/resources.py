"""Locate the bundled catalog resources in source and installed layouts."""

from __future__ import annotations

from pathlib import Path

PACKAGE_DATA_DIR = Path(__file__).resolve().parent / "data"
PROJECT_DATA_DIR = Path("data")


def default_data_path(filename: str) -> Path:
    """Return a working-directory override or the bundled package resource."""

    working_directory_path = PROJECT_DATA_DIR / filename
    if working_directory_path.is_file():
        return working_directory_path
    return PACKAGE_DATA_DIR / filename


DEFAULT_CATALOG_PATH = default_data_path("cards.json")
DEFAULT_DESCRIPTIONS_PATH = default_data_path("mcc_descriptions.json")
