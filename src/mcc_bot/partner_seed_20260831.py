"""Apply the reviewed 2026-08-31 partner correction package once."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .partner_rewards import PartnerRepository, PartnerRewardError
from .partner_seed_20260830 import apply_partner_seed as _apply_partner_seed
from .resources import default_data_path
from .stores import StoreError, StoreRepository

SEED_PATH = default_data_path("partner_seed_20260831.json")


def apply_partner_seed(
    stores: StoreRepository,
    partners: PartnerRepository,
    *,
    actor_id: int,
    path: Path = SEED_PATH,
) -> dict[str, int]:
    """Insert only missing rows from the dated static correction package."""

    return _apply_partner_seed(stores, partners, actor_id=actor_id, path=path)


def main(argv: list[str] | None = None) -> None:
    """Apply the static correction package to the configured stores database."""

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("MCC_STORES_PATH", "var/stores.sqlite3")),
    )
    parser.add_argument("--seed", type=Path, default=SEED_PATH)
    args = parser.parse_args(argv)
    try:
        actor_id = int(os.environ.get("BOT_OWNER_TELEGRAM_ID", ""))
    except ValueError as exc:
        raise SystemExit("BOT_OWNER_TELEGRAM_ID must be a positive integer") from exc
    stores = StoreRepository(args.database)
    stores.initialize()
    partners = PartnerRepository(stores)
    try:
        result = apply_partner_seed(stores, partners, actor_id=actor_id, path=args.seed)
    except (PartnerRewardError, StoreError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
