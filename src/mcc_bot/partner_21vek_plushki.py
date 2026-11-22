"""Remove the bot's cutoff for the reviewed 21vek Vitamin D offer once."""

from __future__ import annotations

import argparse
import os
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from .partner_rewards import PartnerRepository, PartnerRewardError
from .stores import StoreRepository, normalize_store_name

SOURCE_KEY = "plushki:promo:01"
SOURCE_URL = "https://plushki.by/s3/documents/2/Order_of_scoring.pdf"
_AUDIT_ACTION = "21vek_plushki_open_ended"
_STORE_NAMES = {normalize_store_name(name) for name in ("21vek", "21vek.by", "21 век")}


def apply(stores: StoreRepository, *, actor_id: int) -> bool:
    """Apply the one-time data change without overwriting later manual edits."""

    if type(actor_id) is not int or actor_id <= 0:
        raise ValueError("A positive owner ID is required")
    partners = PartnerRepository(stores)
    with stores.transaction() as connection:
        row = connection.execute(
            "SELECT id FROM partner_offers WHERE source_key=?", (SOURCE_KEY,)
        ).fetchone()
        if row is None:
            raise PartnerRewardError("The reviewed 21vek offer was not found")
        offer_id = row["id"]
        if connection.execute(
            "SELECT 1 FROM partner_audit WHERE entity_type='offer' AND entity_id=? AND action=?",
            (offer_id, _AUDIT_ACTION),
        ).fetchone():
            return False

        offer = partners.get_offer(offer_id, connection=connection)
        if offer is None or (offer.card_id, offer.channel, offer.mode, offer.reward_kind) != (
            "vitamin_d",
            "online",
            "additional",
            "points",
        ):
            raise PartnerRewardError("The reviewed 21vek offer identity has changed")
        brand = stores.get_brand(offer.brand_id, connection=connection)
        if brand is None or not _STORE_NAMES.intersection(
            normalize_store_name(name) for name in (brand.name, *brand.aliases)
        ):
            raise PartnerRewardError("The reviewed offer no longer belongs to 21vek")
        if len(offer.tiers) != 1:
            raise PartnerRewardError("The reviewed 21vek offer tiers have changed")
        tier = offer.tiers[0]
        if (tier.value, tier.min_purchase, tier.max_purchase, tier.per_transaction_cap) != (
            Decimal(5),
            Decimal(100),
            None,
            Decimal(50),
        ):
            raise PartnerRewardError("The reviewed 21vek offer terms have changed")

        connection.execute(
            """UPDATE partner_offers
               SET ends_on=NULL, source_url=?, updated_by=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (SOURCE_URL, actor_id, offer.id),
        )
        connection.execute("UPDATE partner_offer_tiers SET ends_on=NULL WHERE id=?", (tier.id,))
        updated = partners.get_offer(offer.id, connection=connection)
        assert updated is not None
        partners._audit(
            connection,
            "offer",
            offer.id,
            _AUDIT_ACTION,
            actor_id,
            {"before": asdict(offer), "after": asdict(updated)},
        )
        return True


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("MCC_STORES_PATH", "var/stores.sqlite3")),
    )
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("The stores database must already exist")
    owner = os.environ.get("BOT_OWNER_TELEGRAM_ID", "")
    if not owner.isdecimal():
        parser.error("BOT_OWNER_TELEGRAM_ID must be a positive integer")
    try:
        changed = apply(StoreRepository(args.database), actor_id=int(owner))
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print("21vek Vitamin D offer updated" if changed else "21vek Vitamin D change already applied")


if __name__ == "__main__":
    main()
