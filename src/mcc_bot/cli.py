"""Catalog validation and local lookup command-line utility."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import suppress
from pathlib import Path

from .catalog import CardCatalog, CatalogError, InvalidMccError
from .config import DEFAULT_CATALOG_PATH
from .formatting import format_matches


def _configure_output() -> None:
    """Keep Russian card names printable on Windows' legacy console code pages."""

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        with suppress(AttributeError, OSError, ValueError):
            reconfigure(encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate or query an MCC moneyback catalog")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_CATALOG_PATH,
        help=f"catalog JSON path (default: {DEFAULT_CATALOG_PATH})",
    )
    parser.add_argument("--mcc", help="optional four-digit MCC to query")
    parser.add_argument("--json", action="store_true", help="emit lookup results as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Validate a catalog and optionally print one lookup."""

    _configure_output()
    args = _parser().parse_args(argv)
    try:
        catalog = CardCatalog.from_file(args.catalog)
        if args.mcc is None:
            print(f"Catalog is valid: {len(catalog.cards)} card(s)")
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
                "mcc": match.offer.mcc,
                "moneyback": str(match.offer.moneyback.value),
                "unit": match.offer.moneyback.unit,
                "currency": match.offer.moneyback.currency,
            }
            for match in matches
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_matches(args.mcc, matches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
