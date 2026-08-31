from __future__ import annotations

from decimal import Decimal

import pytest

from mcc_bot.partner_cleanup_20260830 import PartnerCleanupError, apply_partner_cleanup
from mcc_bot.partner_rewards import PartnerOfferInput, PartnerRepository, PartnerTierInput
from mcc_bot.stores import StoreRepository


def _offer(
    brand_id: int,
    *,
    card_id: str,
    channel: str,
    value: str,
    conditions: str,
    source_url: str,
) -> PartnerOfferInput:
    return PartnerOfferInput(
        brand_id=brand_id,
        card_id=card_id,
        channel=channel,
        mode="total",
        reward_kind="cash",
        tiers=(PartnerTierInput(Decimal(value)),),
        conditions=conditions,
        source_url=source_url,
    )


def _brand(stores: StoreRepository, name: str, channel: str, mcc: str | None):
    payload = {"name": name, "channel": channel}
    if mcc is not None:
        payload["mcc"] = mcc
    result = stores.apply_change("add_merchant", payload, 1)
    assert result.brand_id is not None
    return result


def test_cleanup_merges_catalog_reconciles_untouched_rows_and_is_idempotent(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    # The first partner seed could leave a canonical partner-only card without an
    # MCC while Tannei-backed 21vek.by duplicates held the actual observations.
    canonical_21 = _brand(stores, "21vek", "online", None)
    duplicate_21 = _brand(stores, "21vek.by", "online", "5300")
    _brand(stores, "Инвитро", "offline", "8071")
    invitro_duplicate = _brand(stores, "Инвитро", "offline", "8071")
    green = _brand(stores, "Green", "offline", "5411")
    xistore = _brand(stores, "Xistore.by", "offline", None)
    karat = _brand(stores, 'Сеть ювелирных магазинов "7 карат"', "offline", None)
    partners = PartnerRepository(stores)
    partners.initialize()

    cash_21 = partners.create_offer(
        _offer(
            duplicate_21.brand_id,
            card_id="belgazprombank_cashalot",
            channel="online",
            value="1.5",
            conditions="Повышенный кэшбэк в партнёрской сети Cashalot.",
            source_url="https://cashalot.by/stores/store_select/21vek-by/",
        ),
        actor_id=1,
        source_key="cashalot:21vek-by",
    )
    green_offer = partners.create_offer(
        _offer(
            green.brand_id,
            card_id="belgazprombank_cashalot",
            channel="any",
            value="1.5",
            conditions="Повышенный кэшбэк в партнёрской сети Cashalot.",
            source_url="https://cashalot.by/stores/store_select/green/?city=501",
        ),
        actor_id=1,
        source_key="cashalot:green",
    )
    status = partners.create_offer(
        _offer(
            canonical_21.brand_id,
            card_id="statusbank_statuskarta",
            channel="online",
            value="3.2",
            conditions="Только при онлайн-оплате",
            source_url="https://stbank.by/",
        ),
        actor_id=1,
        source_key="statuskarta:21vek:2026-08-30",
    )
    removed = []
    for result, source_key, value, source_url in (
        (
            xistore,
            "cashalot:xistore-by",
            "3",
            "https://cashalot.by/stores/store_select/xistore-by/",
        ),
        (
            karat,
            "cashalot:7-karat",
            "2",
            "https://cashalot.by/stores/store_select/7-karat/",
        ),
    ):
        removed.append(
            partners.create_offer(
                _offer(
                    result.brand_id,
                    card_id="belgazprombank_cashalot",
                    channel="any",
                    value=value,
                    conditions="Повышенный кэшбэк в партнёрской сети Cashalot.",
                    source_url=source_url,
                ),
                actor_id=1,
                source_key=source_key,
            )
        )
    partners.create_offer(
        _offer(
            karat.brand_id,
            card_id="cactus_mtbank",
            channel="offline",
            value="2",
            conditions="Other card",
            source_url="https://example.test/karat",
        ),
        actor_id=1,
        source_key="other:7-karat",
    )
    with stores.transaction() as connection:
        connection.execute(
            "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?)",
            ("brand:cashalot:21vek", duplicate_21.brand_id),
        )

    first = apply_partner_cleanup(stores, partners, actor_id=1)

    assert first["merges_applied"] >= 2
    assert first["offers_updated"] == 2
    assert first["offers_removed"] == 2
    assert first["empty_merchants_hidden"] == 1
    assert partners.get_offer(cash_21.id, include_archived=True).brand_id == canonical_21.brand_id
    assert partners.get_offer(green_offer.id, include_archived=True).channel == "offline"
    updated_status = partners.get_offer(status.id, include_archived=True)
    assert updated_status.maximum_value == Decimal("2.5")
    assert updated_status.source_url.endswith("/statuskarta-deb/")
    assert all(partners.get_offer(item.id, include_archived=True).archived for item in removed)
    assert stores.search("Xistore.by").matches == ()
    assert stores.search("7 карат").matches
    assert len(stores.search("Инвитро").matches) == 1
    assert stores.get_brand(invitro_duplicate.brand_id) is None

    second = apply_partner_cleanup(stores, partners, actor_id=1)
    assert second["merges_applied"] == 0
    assert second["offers_updated"] == 0
    assert second["offers_removed"] == 0
    assert second["empty_merchants_hidden"] == 0


def test_cleanup_rejects_manual_offer_changes_without_partial_writes(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    green = _brand(stores, "Green", "offline", "5411")
    partners = PartnerRepository(stores)
    partners.initialize()
    offer = partners.create_offer(
        _offer(
            green.brand_id,
            card_id="belgazprombank_cashalot",
            channel="any",
            value="9",
            conditions="Изменено помощником",
            source_url="https://cashalot.by/stores/store_select/green/?city=501",
        ),
        actor_id=2,
        source_key="cashalot:green",
    )

    with pytest.raises(PartnerCleanupError, match="cashalot:green"):
        apply_partner_cleanup(stores, partners, actor_id=1)

    current = partners.get_offer(offer.id, include_archived=True)
    assert current is not None
    assert current.channel == "any"
    assert current.maximum_value == Decimal("9")
    assert current.conditions == "Изменено помощником"


def test_cleanup_combines_reviewed_helix_channels_into_partner_brand(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    offline = _brand(stores, "Helix", "offline", "8071")
    online = _brand(stores, "Helix", "online", "8071")
    partners = PartnerRepository(stores)
    partners.initialize()
    offer = partners.create_offer(
        _offer(
            online.brand_id,
            card_id="cactus_mtbank",
            channel="online",
            value="2",
            conditions="Партнёр Helix",
            source_url="https://example.test/helix",
        ),
        actor_id=1,
        source_key="reviewed:helix",
    )

    first = apply_partner_cleanup(stores, partners, actor_id=1)

    assert first["merges_applied"] == 1
    matches = stores.search("Helix").matches
    assert [brand.id for brand in matches] == [online.brand_id]
    assert {(fact.channel, fact.mcc) for fact in stores.list_brand_mcc(online.brand_id)} == {
        ("offline", "8071"),
        ("online", "8071"),
    }
    assert partners.get_offer(offer.id).brand_id == online.brand_id
    with stores.connection() as connection:
        merged = connection.execute(
            "SELECT archived,merged_into FROM store_brands WHERE id=?", (offline.brand_id,)
        ).fetchone()
    assert tuple(merged) == (1, online.brand_id)
    assert stores.history()[0].kind == "merge_brand"

    second = apply_partner_cleanup(stores, partners, actor_id=1)
    assert second["merges_applied"] == 0


def test_cleanup_merges_only_the_other_explicit_reviewed_networks(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    reviewed = (
        ("Белгосстрах", (("online", ("6300",)), ("offline", ("6300",)))),
        ("Burger King", (("offline", ("5812",)), ("online", ("5814",)))),
        (
            "Белоруснефть",
            (("offline", ("5541", "5542")), ("online", ("5542",))),
        ),
        ("5 Элемент", (("online", ("5732",)), ("offline", ("5732",)))),
        ("DODO Pizza", (("offline", ("5812",)), ("online", ("5812",)))),
        (
            "Domino\N{RIGHT SINGLE QUOTATION MARK}s Pizza",
            (("online", ("5814",)), ("offline", ("5812",))),
        ),
        (
            "Papa John\N{RIGHT SINGLE QUOTATION MARK}s",
            (("online", ("5812",)), ("offline", ("5812",))),
        ),
        ("Суши Весла", (("online", ("5814",)), ("offline", ("5812",)))),
        ("ЧИП и ДИП", (("online", ("5732",)), ("offline", ("5732",)))),
        (
            "Autolight Express",
            (("offline", ("4214", "4215")), ("online", ("4215",))),
        ),
    )
    expected = {}
    for name, merchants in reviewed:
        expected[name] = {(channel, mcc) for channel, mccs in merchants for mcc in mccs}
        for channel, mccs in merchants:
            created = _brand(stores, name, channel, mccs[0])
            for mcc in mccs[1:]:
                stores.apply_change("add_mcc", {"merchant_id": created.merchant_id, "mcc": mcc}, 1)
    _brand(stores, "Билд", "offline", "5211")
    _brand(stores, "Билд", "offline", "5211")
    partners = PartnerRepository(stores)
    partners.initialize()

    result = apply_partner_cleanup(stores, partners, actor_id=1)

    assert result["merges_applied"] == len(reviewed)
    for name, signature in expected.items():
        matches = stores.search(name).matches
        assert len(matches) == 1
        assert {
            (fact.channel, fact.mcc) for fact in stores.list_brand_mcc(matches[0].id)
        } == signature
    assert len(stores.search("Билд").matches) == 2


def test_cleanup_does_not_merge_reviewed_name_with_wrong_mcc_signature(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    offline = _brand(stores, "Helix", "offline", "8071")
    online = _brand(stores, "Helix", "online", "5999")
    partners = PartnerRepository(stores)
    partners.initialize()

    result = apply_partner_cleanup(stores, partners, actor_id=1)

    assert result["merges_applied"] == 0
    assert {brand.id for brand in stores.search("Helix").matches} == {
        offline.brand_id,
        online.brand_id,
    }
    assert stores.get_brand(offline.brand_id) is not None
    assert stores.get_brand(online.brand_id) is not None


def test_cleanup_skips_one_partner_conflict_and_merges_other_reviewed_groups(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    canonical = _brand(stores, "21vek", "online", "5300")
    duplicate = _brand(stores, "21vek.by", "online", "5300")
    helix_offline = _brand(stores, "Helix", "offline", "8071")
    helix_online = _brand(stores, "Helix", "online", "8071")
    partners = PartnerRepository(stores)
    partners.initialize()
    human = partners.create_offer(
        _offer(
            canonical.brand_id,
            card_id="belgazprombank_cashalot",
            channel="any",
            value="1.01",
            conditions="Добавлено владельцем",
            source_url="",
        ),
        actor_id=1,
    )
    official = partners.create_offer(
        _offer(
            duplicate.brand_id,
            card_id="belgazprombank_cashalot",
            channel="online",
            value="1.5",
            conditions="Повышенный кэшбэк в партнёрской сети Cashalot.",
            source_url="https://cashalot.by/stores/store_select/21vek-by/",
        ),
        actor_id=1,
        source_key="cashalot:21vek-by",
    )

    result = apply_partner_cleanup(stores, partners, actor_id=1)

    assert result["merge_conflicts"] == 1
    assert result["merges_applied"] == 1
    assert {brand.id for brand in stores.search("21vek").matches} == {canonical.brand_id}
    assert {brand.id for brand in stores.search("21vek.by").matches} == {duplicate.brand_id}
    assert [brand.id for brand in stores.search("Helix").matches] == [helix_offline.brand_id]
    assert stores.get_brand(helix_online.brand_id) is None
    assert partners.get_offer(human.id, include_archived=True).brand_id == canonical.brand_id
    assert partners.get_offer(official.id, include_archived=True).brand_id == duplicate.brand_id
