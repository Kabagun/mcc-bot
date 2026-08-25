"""Catalog validation and local lookup command-line utility."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import suppress
from pathlib import Path

from .catalog import CardCatalog, CatalogError, InvalidMccError
from .config import DEFAULT_CATALOG_PATH, DEFAULT_DESCRIPTIONS_PATH
from .descriptions import DescriptionCatalog
from .formatting import format_matches


def _configure_output() -> None:
    """Keep Russian card names printable on Windows' legacy console code pages."""

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        with suppress(AttributeError, OSError, ValueError):
            reconfigure(encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Проверить или запросить каталог манибэка MCC")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_CATALOG_PATH,
        help=f"путь к каталогу JSON (по умолчанию: {DEFAULT_CATALOG_PATH})",
    )
    parser.add_argument(
        "--descriptions",
        type=Path,
        default=DEFAULT_DESCRIPTIONS_PATH,
        help=f"путь к описаниям MCC (по умолчанию: {DEFAULT_DESCRIPTIONS_PATH})",
    )
    parser.add_argument("--mcc", help="необязательный четырёхзначный MCC")
    parser.add_argument("--json", action="store_true", help="вывести результат в JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Validate a catalog and optionally print one lookup."""

    _configure_output()
    args = _parser().parse_args(argv)
    try:
        catalog = CardCatalog.from_file(args.catalog)
        descriptions = DescriptionCatalog.from_file(args.descriptions)
        if args.mcc is None:
            print(f"Каталог корректен: карт — {len(catalog.cards)}")
            return 0
        matches = catalog.lookup(args.mcc)
    except (CatalogError, InvalidMccError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        payload = [
            {
                "id": match.card.id,
                "name": match.card.name,
                "issuer": match.card.issuer,
                "mcc": match.mcc,
                "rewards": [
                    {
                        "program_id": component.program_id,
                        "kind": component.kind,
                        "gross_percent": str(component.gross_percent),
                        "net_percent": str(component.net_percent),
                        "tax_exempt": component.tax_exempt,
                    }
                    for component in match.components
                ],
            }
            for match in matches
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_matches(args.mcc, matches, descriptions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
