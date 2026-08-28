from __future__ import annotations

import re

import mcc_bot.curated_stores_20260828 as curated
from mcc_bot.curated_stores_20260828 import (
    BRAND_METADATA,
    CONFIRMATIONS,
    EXISTING_VARIANTS,
    HELD_ROWS,
    IMAGE_NEW,
    IMAGE_OLD_1,
    IMAGE_OLD_2,
    NEW_VARIANTS,
    Confirmation,
    ExistingVariant,
    NewVariant,
)
from mcc_bot.stores import StoreRepository


def test_every_supplied_table_row_is_either_accepted_or_held() -> None:
    expected = {IMAGE_OLD_1: 16, IMAGE_OLD_2: 14, IMAGE_NEW: 60}

    for image_hash, row_count in expected.items():
        accepted = {item.row for item in CONFIRMATIONS if item.image_sha256 == image_hash}
        held = set(HELD_ROWS[image_hash])

        assert accepted.isdisjoint(held)
        assert accepted | held == set(range(1, row_count + 1))


def test_confirmation_keys_are_stable_unique_and_notes_fit_public_limit() -> None:
    keys = [item.source_key for item in CONFIRMATIONS]

    assert len(keys) == len(set(keys)) == 70
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}:row:[1-9][0-9]*", key) for key in keys)
    assert all(len(item.note) <= 48 for item in CONFIRMATIONS)


def test_all_confirmation_and_metadata_variant_keys_are_declared() -> None:
    declared = set(EXISTING_VARIANTS) | {item.key for item in NEW_VARIANTS}

    assert {item.variant for item in CONFIRMATIONS} <= declared
    assert set(BRAND_METADATA) <= declared
    assert all(item.group_with is None or item.group_with in declared for item in NEW_VARIANTS)


def test_curated_update_is_one_transaction_and_safe_to_rerun(tmp_path, monkeypatch) -> None:
    repository = StoreRepository(tmp_path / "stores.sqlite3")
    repository.initialize()
    base = repository.apply_change(
        "add_merchant", {"name": "Base", "channel": "offline"}, actor_id=1
    )
    image_hash = "a" * 64
    monkeypatch.setattr(
        curated,
        "EXISTING_VARIANTS",
        {"base": ExistingVariant(base.merchant_id, "Base")},
    )
    monkeypatch.setattr(curated, "MERCHANT_MERGES", ())
    monkeypatch.setattr(
        curated,
        "NEW_VARIANTS",
        (NewVariant("online", "Base online", "online", "base"),),
    )
    monkeypatch.setattr(curated, "BRAND_METADATA", {"base": ("Base", ("Alias",))})
    monkeypatch.setattr(
        curated,
        "CONFIRMATIONS",
        (Confirmation("online", "5300", image_hash, 1, "Онлайн-оплата"),),
    )
    monkeypatch.setattr(curated, "HELD_ROWS", {image_hash: ()})

    first = curated.apply_curated_update(repository, actor_id=2)
    second = curated.apply_curated_update(repository, actor_id=2)

    assert first["variants_created"] == first["confirmations_added"] == 1
    assert second["variants_created"] == second["confirmations_added"] == 0
    assert second["variants_existing"] == second["confirmations_existing"] == 1
    brand = repository.search("Alias").matches[0]
    assert set(repository.list_brand_channels(brand.id)) == {"offline", "online"}
    assert [
        (fact.mcc, fact.note) for fact in repository.list_brand_mcc(brand.id, channel="online")
    ] == [("5300", "Онлайн-оплата")]
