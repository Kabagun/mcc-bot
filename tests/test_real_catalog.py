from __future__ import annotations

import json
import re
from decimal import Decimal

from mcc_bot.catalog import CardCatalog
from mcc_bot.config import DEFAULT_CATALOG_PATH, DEFAULT_DESCRIPTIONS_PATH
from mcc_bot.descriptions import DescriptionCatalog
from mcc_bot.formatting import format_limits, format_match_pages, format_matches, format_moneyback

DATA_PATH = DEFAULT_CATALOG_PATH
DESCRIPTION_PATH = DEFAULT_DESCRIPTIONS_PATH
KUFAR_EXCLUSIONS = {
    "6010",
    "6011",
    "6012",
    "4900",
    "4814",
    "4829",
    "4899",
    "5960",
    "6050",
    "6051",
    "6211",
    "6531",
    "6536",
    "6537",
    "6538",
    "6539",
    "6540",
    "7311",
    "7399",
    "8398",
    "8999",
    "9222",
    "9223",
    "9311",
    "9399",
    "7995",
    "9402",
}
YARKAYA_EXCLUSIONS = {
    "6010",
    "6011",
    "6012",
    "9402",
    "4900",
    "6536",
    "6537",
    "6538",
    "6540",
    "4829",
    "6051",
    "6532",
    "7995",
    "5933",
    "4813",
    "4814",
    "4816",
    "4899",
    "9222",
    "9311",
    "7276",
    "9211",
    "6211",
    "6300",
    "5960",
    "8211",
    "8220",
    "8351",
    "8661",
    "8398",
    "8641",
    "8651",
    "8675",
    "8699",
    "9405",
    "9399",
    "8999",
}
VITAMIN_POINTS_EXCLUSIONS = KUFAR_EXCLUSIONS | {"4111", "4112"}
R_KARTA_EXCLUSIONS = {
    "4812",
    "4814",
    "4816",
    "4829",
    "4900",
    "5960",
    "6010",
    "6011",
    "6012",
    "6028",
    "6050",
    "6051",
    "6211",
    "6300",
    "6399",
    "6529",
    "6530",
    "6531",
    "6532",
    "6533",
    "6534",
    "6535",
    "6536",
    "6537",
    "6538",
    "6540",
    "7299",
    "7311",
    "7372",
    "7399",
    "7800",
    "7801",
    "7802",
    "7995",
    "8999",
    "9222",
    "9311",
    "9399",
    "9402",
    "9406",
    "9700",
    "9701",
    "9702",
    "9754",
}
CASHALOT_EXCLUSIONS = {
    "4111",
    "4112",
    "4131",
    "4511",
    "4814",
    "4816",
    "4821",
    "4829",
    "4899",
    "4900",
    "5541",
    "5542",
    "6010",
    "6011",
    "6012",
    "6051",
    "6211",
    "6536",
    "6537",
    "6538",
    "7523",
    "7995",
    "8398",
    "9211",
    "9222",
    "9223",
    "9311",
    "9399",
    "9402",
    "9406",
}
STATUSKARTA_EXCLUSIONS = {
    "4812",
    "4814",
    "4816",
    "4900",
    "6012",
    "6028",
    "6051",
    "6211",
    "6536",
    "6537",
    "6538",
    "6540",
    "7311",
    "7372",
    "7399",
    "7995",
    "8999",
    "9311",
    "9399",
    "9402",
}
BNB_1_2_3_EXCLUSIONS = {
    "4814",
    "4829",
    "4900",
    "6012",
    "6028",
    "6050",
    "6051",
    "6211",
    "6531",
    "6532",
    "6533",
    "6534",
    "6535",
    "6536",
    "6537",
    "6538",
    "6539",
    "6540",
    "7311",
    "7399",
    "7995",
    "8999",
    "9211",
    "9222",
    "9311",
    "9399",
    "9402",
}
COMBO_EXCLUSIONS = {
    "4812",
    "4814",
    "4829",
    "4899",
    "4900",
    "5960",
    "6010",
    "6011",
    "6012",
    "6028",
    "6050",
    "6051",
    "6211",
    "6300",
    "6529",
    "6530",
    "6531",
    "6532",
    "6533",
    "6534",
    "6535",
    "6536",
    "6537",
    "6538",
    "6539",
    "6540",
    "7299",
    "7311",
    "7399",
    "7800",
    "7801",
    "7802",
    "7995",
    "8398",
    "8999",
    "9222",
    "9223",
    "9311",
    "9399",
    "9402",
    "9406",
    "9700",
    "9701",
    "9702",
}
SPRAUNAYA_MCCS = {
    "2741",
    "4111",
    "4112",
    "4121",
    "4131",
    "4511",
    "4722",
    "5039",
    "5047",
    "5111",
    "5122",
    "5131",
    "5191",
    "5193",
    "5261",
    "5292",
    "5295",
    "5411",
    "5541",
    "5542",
    "5621",
    "5641",
    "5651",
    "5655",
    "5712",
    "5811",
    "5812",
    "5813",
    "5814",
    "5912",
    "5941",
    "5942",
    "5945",
    "5949",
    "5970",
    "5975",
    "5976",
    "5996",
    "7011",
    "7221",
    "7297",
    "7298",
    "7523",
    "7832",
    "7932",
    "7933",
    "7941",
    "7991",
    "7996",
    "7997",
    "7998",
    "8011",
    "8021",
    "8043",
    "8062",
    "8071",
    "8099",
    "8299",
    "8351",
}
SPRAUNAYA_EXCLUSIONS = {
    "4814",
    "4900",
    "6010",
    "6011",
    "6012",
    "6051",
    "6211",
    "6300",
    "7276",
    "7995",
    "8999",
    "9311",
    "9402",
}
IZI_MCCS = {"5811", "5812", "5813", "5814", "7832", "7922", "7929", "7932", "7933", "7991"}
SUPPORTED_CARD_KEYS = {
    "id",
    "name",
    "issuer",
    "emoji",
    "condition",
    "reward_limits",
    "reward_programs",
}
SUPPORTED_PROGRAM_KEYS = {
    "id",
    "kind",
    "tax_exempt",
    "offers",
    "rules",
    "default",
    "excluded_mccs",
    "minimum_payment",
    "maximum_reward",
    "monthly_maximum_not_defined",
    "maximum_reward_alternatives",
    "domestic_country",
    "foreign_value",
}


def _catalog() -> CardCatalog:
    return CardCatalog.from_file(DATA_PATH)


def _descriptions() -> DescriptionCatalog:
    return DescriptionCatalog.from_file(DESCRIPTION_PATH)


def test_real_catalog_has_expected_card_and_offer_counts() -> None:
    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))

    assert list(raw) == ["version", "cards"]
    assert raw["version"] == 2
    assert len(raw["cards"]) == 18
    assert (
        sum(
            len(program.get("offers", []))
            for card in raw["cards"]
            for program in card["reward_programs"]
        )
        == 2887
    )
    assert all(set(card) <= SUPPORTED_CARD_KEYS for card in raw["cards"])
    assert all(
        set(program) <= SUPPORTED_PROGRAM_KEYS
        for card in raw["cards"]
        for program in card["reward_programs"]
    )
    assert all("notes" not in card for card in raw["cards"])
    assert all(
        "notes" not in offer
        for card in raw["cards"]
        for program in card["reward_programs"]
        for offer in program.get("offers", [])
    )
    assert all(card.get("issuer") for card in raw["cards"])
    kufar = next(card for card in raw["cards"] if card["id"] == "kufar")
    social = next(card for card in raw["cards"] if card["id"] == "mtbank_social")
    vitamin = next(card for card in raw["cards"] if card["id"] == "vitamin_d")
    cashalot = next(card for card in raw["cards"] if card["id"] == "belgazprombank_cashalot")
    statuskarta = next(card for card in raw["cards"] if card["id"] == "statusbank_statuskarta")
    bnb_1_2_3 = next(card for card in raw["cards"] if card["id"] == "bnb_1_2_3")
    spraunaya = next(card for card in raw["cards"] if card["id"] == "dabrabyt_spraunaya")
    izi = next(card for card in raw["cards"] if card["id"] == "belarusbank_izi")
    combo = next(card for card in raw["cards"] if card["id"] == "paritet_combo")
    r_karta = next(card for card in raw["cards"] if card["id"] == "reshenie_r_karta")
    yarkaya = next(card for card in raw["cards"] if card["id"] == "yarkaya_karta")
    assert set(kufar["reward_programs"][0]["excluded_mccs"]) == KUFAR_EXCLUSIONS
    assert social["reward_programs"][0]["default"] == {"value": 1}
    assert set(social["reward_programs"][0]["excluded_mccs"]) == KUFAR_EXCLUSIONS
    assert set(vitamin["reward_programs"][1]["excluded_mccs"]) == VITAMIN_POINTS_EXCLUSIONS
    assert set(cashalot["reward_programs"][0]["excluded_mccs"]) == CASHALOT_EXCLUSIONS
    assert set(statuskarta["reward_programs"][0]["excluded_mccs"]) == STATUSKARTA_EXCLUSIONS
    assert set(bnb_1_2_3["reward_programs"][0]["excluded_mccs"]) == BNB_1_2_3_EXCLUSIONS
    spraunaya_program = spraunaya["reward_programs"][0]
    assert len(spraunaya_program["offers"]) == len(SPRAUNAYA_MCCS) == 59
    assert {offer["mcc"] for offer in spraunaya_program["offers"]} == SPRAUNAYA_MCCS
    assert {offer["value"] for offer in spraunaya_program["offers"]} == {1.11}
    assert set(spraunaya_program["excluded_mccs"]) == SPRAUNAYA_EXCLUSIONS
    izi_program = izi["reward_programs"][0]
    assert len(izi_program["offers"]) == len(IZI_MCCS) == 10
    assert {offer["mcc"] for offer in izi_program["offers"]} == IZI_MCCS
    assert {offer["value"] for offer in izi_program["offers"]} == {2}
    combo_program = combo["reward_programs"][0]
    assert combo_program["default"] == {"value": 1.2}
    assert len(combo_program["excluded_mccs"]) == len(COMBO_EXCLUSIONS) == 44
    assert set(combo_program["excluded_mccs"]) == COMBO_EXCLUSIONS
    assert r_karta["reward_programs"][0]["default"] == {"value": 1.5}
    assert set(r_karta["reward_programs"][0]["excluded_mccs"]) == R_KARTA_EXCLUSIONS
    yarkaya_exclusions = yarkaya["reward_programs"][0]["excluded_mccs"]
    assert len(yarkaya_exclusions) == len(YARKAYA_EXCLUSIONS) == 37
    assert set(yarkaya_exclusions) == YARKAYA_EXCLUSIONS


def test_real_catalog_5411_has_expected_sorted_output() -> None:
    matches = _catalog().lookup("5411")

    assert [match.card.id for match in matches] == [
        "vitamin_d",
        "oplati",
        "shopper_mtbank",
        "zepter_card",
        "zepter_plus",
        "reshenie_r_karta",
        "paritet_combo",
        "dabrabyt_spraunaya",
        "bnb_1_2_3",
        "belveb_dvizhenie",
        "kufar",
        "mtkarta",
        "mtbank_social",
        "yarkaya_karta",
        "belgazprombank_cashalot",
        "statusbank_statuskarta",
    ]
    vitamin = matches[0]
    assert vitamin.gross_percent == Decimal("4")
    assert [component.gross_percent for component in vitamin.components] == [
        Decimal("1"),
        Decimal("3"),
    ]
    assert [component.net_percent for component in vitamin.components] == [
        Decimal("1"),
        Decimal("3"),
    ]
    assert format_moneyback(vitamin) == "1% + 3% баллами"

    shopper = next(match for match in matches if match.card.id == "shopper_mtbank")
    assert shopper.gross_percent == Decimal("2.5")
    assert format_moneyback(shopper) == "2,5% (2,44%)"
    oplati = next(match for match in matches if match.card.id == "oplati")
    assert oplati.gross_percent == Decimal("3")
    assert oplati.components[0].kind == "cash"
    assert oplati.components[0].tax_exempt is True
    zepter_card = next(match for match in matches if match.card.id == "zepter_card")
    assert zepter_card.gross_percent == Decimal("2")
    zepter = next(match for match in matches if match.card.id == "zepter_plus")
    assert zepter.gross_percent == Decimal("2")
    kufar = next(match for match in matches if match.card.id == "kufar")
    assert kufar.gross_percent == Decimal("1")
    assert kufar.components[0].kind == "points"
    assert format_moneyback(kufar) == "1% баллами"

    rendered = format_matches("5411", matches, _descriptions())
    assert rendered == (
        "🛒 MCC 5411 — Продуктовые магазины\n\n"
        "1. 💳 Витамин Д — 1% + 3% баллами\n"
        "2. 💳 Оплати — 3%\n"
        "3. 💳 Шоппер — 2,5% (2,44%)\n"
        "4. 💳 Цептер Card — 2%\n"
        "5. 💳 Цептер PLUS — 2%\n"
        "6. 💳 R-карта — 1,5%\n"
        "7. 💳 КОМБОкарта — 1,2%\n"
        "8. 💳 Спраўная — 1,11%\n"
        "9. 💳 1-2-3 — 1%\n"
        "10. 💳 Движение — 1%\n"
        "11. 💳 Куфар — 1% баллами\n"
        "12. 💳 МТкарта — 1% баллами\n"
        "13. 💳 Социальная — 1%\n"
        "14. 💳 Яркая — 1%\n"
        "15. 💳 Cashalot — 0,5%\n"
        "16. 💳 Статускарта — 0,5%"
    )


def test_real_catalog_new_cards_have_exact_program_shapes_and_no_duplicates() -> None:
    cards = {
        card["id"]: card for card in json.loads(DATA_PATH.read_text(encoding="utf-8"))["cards"]
    }

    zepter_card = cards["zepter_card"]
    zepter_offers = zepter_card["reward_programs"][0]["offers"]
    assert len(zepter_offers) == 39
    assert {offer["mcc"] for offer in zepter_offers} == {
        "3351",
        "3501",
        "4111",
        "4112",
        "4121",
        "4511",
        "5094",
        "5262",
        "5300",
        "5309",
        "5311",
        "5411",
        "5462",
        "5499",
        "5532",
        "5541",
        "5542",
        "5552",
        "5719",
        "5732",
        "5812",
        "5814",
        "5912",
        "5945",
        "5962",
        "5977",
        "5995",
        "5999",
        "7011",
        "7298",
        "7512",
        "7534",
        "7538",
        "7542",
        "7832",
        "7996",
        "7998",
        "7999",
        "8099",
    }
    assert {offer["value"] for offer in zepter_offers} == {2}
    zepter_program = zepter_card["reward_programs"][0]
    assert zepter_program["domestic_country"] == "BY"
    assert zepter_program["foreign_value"] == 1
    assert zepter_program["maximum_reward"] == {
        "amount": 150,
        "unit": "currency",
        "currency": "BYN",
    }
    assert zepter_program["maximum_reward_alternatives"] == [
        {"amount": 50, "unit": "currency", "currency": "USD"},
        {"amount": 50, "unit": "currency", "currency": "EUR"},
    ]

    oplati = cards["oplati"]
    exempt_program, one_percent_program = oplati["reward_programs"]
    exempt_mccs = {offer["mcc"] for offer in exempt_program["offers"]}
    one_percent_mccs = {offer["mcc"] for offer in one_percent_program["offers"]}
    assert exempt_program["kind"] == "cash"
    assert exempt_program["tax_exempt"] is True
    assert exempt_mccs == {"5411", "5422", "5441", "5451", "5462", "5499"}
    assert len(one_percent_mccs) == len(one_percent_program["offers"]) == 262
    assert exempt_mccs.isdisjoint(one_percent_mccs)
    assert "5921" not in exempt_mccs | one_percent_mccs
    assert all(match.card.id != "oplati" for match in _catalog().lookup("5921"))
    assert all(program.get("default") is None for program in oplati["reward_programs"])
    assert all(offer["value"] == 1 for offer in one_percent_program["offers"])

    yarkaya_program = cards["yarkaya_karta"]["reward_programs"][0]
    assert yarkaya_program["kind"] == "cash"
    assert yarkaya_program["default"] == {"value": 1}
    assert yarkaya_program.get("offers", []) == []
    assert set(yarkaya_program["excluded_mccs"]) == YARKAYA_EXCLUSIONS

    cashalot_program = cards["belgazprombank_cashalot"]["reward_programs"][0]
    assert cashalot_program["default"] == {"value": 1}
    assert cashalot_program["offers"] == [{"mcc": "5411", "value": 0.5}]

    statuskarta_program = cards["statusbank_statuskarta"]["reward_programs"][0]
    assert statuskarta_program["default"] == {"value": 1.5}
    assert statuskarta_program["offers"] == [
        {"mcc": "5411", "value": 0.5},
        {"mcc": "5300", "value": 0.5},
        {"mcc": "5541", "value": 0.5},
    ]

    bnb_program = cards["bnb_1_2_3"]["reward_programs"][0]
    assert bnb_program["default"] == {"value": 1}
    assert bnb_program.get("offers", []) == []

    for card_id in (
        "zepter_card",
        "oplati",
        "belgazprombank_cashalot",
        "statusbank_statuskarta",
    ):
        for program in cards[card_id]["reward_programs"]:
            mccs = [offer["mcc"] for offer in program["offers"]]
            assert len(mccs) == len(set(mccs))


def test_real_catalog_5912_has_taxed_five_percent_without_internal_conditions() -> None:
    matches = _catalog().lookup("5912")
    social = next(match for match in matches if match.card.id == "mtbank_social")
    assert social.gross_percent == Decimal("5")
    assert social.net_percent == Decimal("4.61")
    assert format_moneyback(social) == "5% (4,61%)"
    assert next(match for match in matches if match.card.id == "mtkarta").card.condition.kind == (
        "max_connected_categories"
    )
    assert next(
        match for match in matches if match.card.id == "cactus_mtbank"
    ).card.condition.kind == ("selected_category")
    rendered = format_matches("5912", matches, _descriptions())
    assert "💳 Движение — 5% (4,61%)" in rendered
    assert "подключённых категорий" not in rendered
    assert "выбранная категория" not in rendered


def test_real_catalog_5411_has_exact_rich_output_without_changing_rewards() -> None:
    matches = _catalog().lookup("5411")
    rendered = format_matches("5411", matches, _descriptions(), html=True)

    assert rendered == (
        "<b>🛒 MCC 5411 — Продуктовые магазины</b>\n\n"
        "1. 💳 <b>Витамин Д</b> — 1% + 3% баллами\n"
        "2. 💳 <b>Оплати</b> — 3%\n"
        "3. 💳 <b>Шоппер</b> — 2,5% (2,44%)\n"
        "4. 💳 <b>Цептер Card</b> — 2%\n"
        "5. 💳 <b>Цептер PLUS</b> — 2%\n"
        "6. 💳 <b>R-карта</b> — 1,5%\n"
        "7. 💳 <b>КОМБОкарта</b> — 1,2%\n"
        "8. 💳 <b>Спраўная</b> — 1,11%\n"
        "9. 💳 <b>1-2-3</b> — 1%\n"
        "10. 💳 <b>Движение</b> — 1%\n"
        "11. 💳 <b>Куфар</b> — 1% баллами\n"
        "12. 💳 <b>МТкарта</b> — 1% баллами\n"
        "13. 💳 <b>Социальная</b> — 1%\n"
        "14. 💳 <b>Яркая</b> — 1%\n"
        "15. 💳 <b>Cashalot</b> — 0,5%\n"
        "16. 💳 <b>Статускарта</b> — 0,5%"
    )
    pages = format_match_pages("5411", matches, _descriptions(), html=True)
    assert len(pages) == 1
    assert pages[0].compact == rendered
    assert pages[0].expanded == format_matches(
        "5411", matches, _descriptions(), details=True, html=True
    )
    assert "   <i>Белинвестбанк</i>\n" in pages[0].expanded
    assert "\u20e3" not in pages[0].compact + pages[0].expanded
    assert "Деньги: мин. платёж 10 BYN · макс. 50 BYN/мес." in pages[0].expanded
    assert "Баллы: без минимума · макс. 200 баллов/мес." in pages[0].expanded


def test_real_catalog_social_uses_one_percent_fallback_with_kufar_exclusions() -> None:
    catalog = _catalog()
    matches = catalog.lookup("7297")

    social = next(match for match in matches if match.card.id == "mtbank_social")
    assert social.gross_percent == Decimal("1")
    assert social.net_percent == Decimal("1")
    assert format_moneyback(social) == "1%"
    assert all(match.card.id != "mtbank_social" for match in catalog.lookup("6010"))


def test_real_catalog_r_karta_default_and_exclusions() -> None:
    catalog = _catalog()
    r_karta = next(match for match in catalog.lookup("7297") if match.card.id == "reshenie_r_karta")
    assert r_karta.gross_percent == Decimal("1.5")
    assert format_moneyback(r_karta) == "1,5%"
    assert all(match.card.id != "reshenie_r_karta" for match in catalog.lookup("4812"))


def test_real_catalog_cashalot_default_override_and_exclusions() -> None:
    catalog = _catalog()
    default = next(
        match for match in catalog.lookup("7297") if match.card.id == "belgazprombank_cashalot"
    )
    grocery = next(
        match for match in catalog.lookup("5411") if match.card.id == "belgazprombank_cashalot"
    )

    assert default.gross_percent == Decimal("1")
    assert grocery.gross_percent == Decimal("0.5")
    assert all(match.card.id != "belgazprombank_cashalot" for match in catalog.lookup("4900"))


def test_real_catalog_statuskarta_default_overrides_and_exclusions() -> None:
    catalog = _catalog()
    default = next(
        match for match in catalog.lookup("7297") if match.card.id == "statusbank_statuskarta"
    )

    assert default.gross_percent == Decimal("1.5")
    for mcc in ("5411", "5300", "5541"):
        match = next(
            match for match in catalog.lookup(mcc) if match.card.id == "statusbank_statuskarta"
        )
        assert match.gross_percent == Decimal("0.5")
    assert all(match.card.id != "statusbank_statuskarta" for match in catalog.lookup("4812"))


def test_real_catalog_bnb_1_2_3_default_and_exclusions() -> None:
    catalog = _catalog()
    grocery = next(match for match in catalog.lookup("5411") if match.card.id == "bnb_1_2_3")

    assert grocery.gross_percent == Decimal("1")
    assert format_moneyback(grocery) == "1%"
    assert all(match.card.id != "bnb_1_2_3" for match in catalog.lookup("4814"))


def test_real_catalog_combo_default_and_exclusions() -> None:
    catalog = _catalog()
    grocery = next(match for match in catalog.lookup("5411") if match.card.id == "paritet_combo")

    assert grocery.gross_percent == Decimal("1.2")
    assert format_moneyback(grocery) == "1,2%"
    assert all(match.card.id != "paritet_combo" for match in catalog.lookup("4812"))


def test_real_catalog_spraunaya_uses_only_listed_mccs_and_visible_exclusions() -> None:
    catalog = _catalog()
    match = next(match for match in catalog.lookup("5411") if match.card.id == "dabrabyt_spraunaya")

    assert match.gross_percent == Decimal("1.11")
    assert format_moneyback(match) == "1,11%"
    assert all(match.card.id != "dabrabyt_spraunaya" for match in catalog.lookup("7995"))
    assert all(match.card.id != "dabrabyt_spraunaya" for match in catalog.lookup("1234"))


def test_real_catalog_izi_uses_two_percent_for_only_listed_mccs() -> None:
    catalog = _catalog()
    match = next(match for match in catalog.lookup("5812") if match.card.id == "belarusbank_izi")

    assert match.gross_percent == Decimal("2")
    assert format_moneyback(match) == "2%"
    assert all(match.card.id != "belarusbank_izi" for match in catalog.lookup("5411"))
    rendered = format_matches("5812", catalog.lookup("5812"), _descriptions())
    assert "💳 Изи-карта — 2%" in rendered
    assert "Беларусбанк" not in rendered
    assert "мин. платёж" not in rendered


def test_real_catalog_vitamin_points_exclude_transport() -> None:
    catalog = _catalog()
    assert all(match.card.id != "vitamin_d" for match in catalog.lookup("4111"))
    vitamin = next(match for match in catalog.lookup("4112") if match.card.id == "vitamin_d")
    assert [component.kind for component in vitamin.components] == ["cash"]
    assert format_moneyback(vitamin) == "1%"


def test_real_catalog_kufar_fallback_exclusions_vitamin_and_leading_zero() -> None:
    catalog = _catalog()

    assert all(
        match.card.id not in {"kufar", "vitamin_d", "yarkaya_karta"}
        for match in catalog.lookup("6010")
    )
    fallback = next(match for match in catalog.lookup("1234") if match.card.id == "kufar")
    assert fallback.gross_percent == Decimal("2")
    vitamin = next(match for match in catalog.lookup("1234") if match.card.id == "vitamin_d")
    assert vitamin.components[0].kind == "points"
    assert vitamin.gross_percent == Decimal("3")
    vitamin_zero = next(match for match in catalog.lookup("0742") if match.card.id == "vitamin_d")
    assert [component.gross_percent for component in vitamin_zero.components] == [
        Decimal("1"),
        Decimal("3"),
    ]
    yarkaya = next(match for match in catalog.lookup("1234") if match.card.id == "yarkaya_karta")
    assert yarkaya.gross_percent == Decimal("1")


def test_real_catalog_yarkaya_replaces_old_exclusions_with_requested_table() -> None:
    catalog = _catalog()

    assert all(match.card.id != "yarkaya_karta" for match in catalog.lookup("4813"))
    restored = next(match for match in catalog.lookup("6050") if match.card.id == "yarkaya_karta")
    assert restored.gross_percent == Decimal("1")


def test_real_catalog_uses_requested_display_metadata() -> None:
    cards = {card.id: card for card in _catalog().cards}

    assert cards["belveb_dvizhenie"].name == "Движение"
    assert cards["belveb_dvizhenie"].issuer == "Банк БелВЭБ"
    expected_markers = {
        "belveb_dvizhenie": "💳",
        "zepter_plus": "💳",
        "mtkarta": "💳",
        "mtbank_social": "💳",
        "shopper_mtbank": "💳",
        "cactus_mtbank": "💳",
        "vitamin_d": "💳",
        "kufar": "💳",
        "zepter_card": "💳",
        "oplati": "💳",
        "paritet_combo": "💳",
        "dabrabyt_spraunaya": "💳",
        "belarusbank_izi": "💳",
        "bnb_1_2_3": "💳",
        "belgazprombank_cashalot": "💳",
        "statusbank_statuskarta": "💳",
        "reshenie_r_karta": "💳",
        "yarkaya_karta": "💳",
    }
    assert {card_id: cards[card_id].emoji for card_id in expected_markers} == expected_markers
    assert cards["zepter_plus"].issuer == "Цептер Банк"
    assert cards["zepter_card"].name == "Цептер Card"
    assert cards["zepter_card"].issuer == "Цептер Банк"
    assert cards["vitamin_d"].issuer == "Белинвестбанк"
    assert cards["oplati"].issuer == "Белинвестбанк"
    assert cards["kufar"].issuer == "МТбанк / Visa / Kufar"
    assert cards["mtkarta"].name == "МТкарта"
    assert cards["mtbank_social"].name == "Социальная"
    assert cards["kufar"].name == "Куфар"
    assert cards["yarkaya_karta"].name == "Яркая"
    assert cards["belgazprombank_cashalot"].name == "Cashalot"
    assert cards["belgazprombank_cashalot"].issuer == "Белгазпромбанк"
    assert cards["statusbank_statuskarta"].name == "Статускарта"
    assert cards["statusbank_statuskarta"].issuer == "СтатусБанк"
    assert cards["bnb_1_2_3"].name == "1-2-3"
    assert cards["bnb_1_2_3"].issuer == "БНБ-Банк"
    assert cards["dabrabyt_spraunaya"].name == "Спраўная"
    assert cards["dabrabyt_spraunaya"].issuer == "Банк Дабрабыт"
    assert cards["belarusbank_izi"].name == "Изи-карта"
    assert cards["belarusbank_izi"].issuer == "Беларусбанк"
    assert cards["paritet_combo"].name == "КОМБОкарта"
    assert cards["paritet_combo"].issuer == "Паритетбанк"
    assert cards["reshenie_r_karta"].issuer == "Банк Решение"
    assert cards["reshenie_r_karta"].name == "R-карта"
    assert cards["yarkaya_karta"].issuer == "Приорбанк"
    for card_id in ("mtkarta", "mtbank_social", "shopper_mtbank", "cactus_mtbank"):
        assert cards[card_id].issuer == "МТбанк"


def test_real_catalog_has_requested_payment_and_reward_limits() -> None:
    cards = {card.id: card for card in _catalog().cards}
    assert all(
        program.minimum_payment is not None
        for card in cards.values()
        for program in card.reward_programs
    )
    for card_id, program_id in (
        ("zepter_card", "cash"),
        ("statusbank_statuskarta", "cash"),
        ("belarusbank_izi", "cash"),
    ):
        program = next(
            program for program in cards[card_id].reward_programs if program.id == program_id
        )
        assert program.minimum_payment is not None
        assert program.minimum_payment.amount == Decimal("0")
        assert program.minimum_payment.currency == "BYN"
    expected = {
        ("belveb_dvizhenie", "cash"): ("0", "50", "currency"),
        ("zepter_plus", "cash"): ("0", "100", "currency"),
        ("zepter_card", "cash"): ("0", "150", "currency"),
        ("mtkarta", "points"): ("10", "200", "points"),
        ("mtbank_social", "cash"): ("0", "100", "currency"),
        ("shopper_mtbank", "cash"): ("10", "30", "currency"),
        ("cactus_mtbank", "cash"): ("0", "300", "points"),
        ("vitamin_d", "cash"): ("10", "50", "currency"),
        ("vitamin_d", "points"): ("0", "200", "points"),
        ("kufar", "points"): ("0", "200", "points"),
        ("reshenie_r_karta", "cash"): ("0", "100", "currency"),
        ("dabrabyt_spraunaya", "cash"): ("0", "100", "currency"),
        ("paritet_combo", "cash"): ("5", "130", "currency"),
        ("belarusbank_izi", "cash"): ("0", "20", "currency"),
        ("bnb_1_2_3", "cash"): ("10", "123", "currency"),
        ("statusbank_statuskarta", "cash"): ("0", "100", "currency"),
    }
    for (card_id, program_id), (minimum, maximum, unit) in expected.items():
        program = next(
            program for program in cards[card_id].reward_programs if program.id == program_id
        )
        assert program.minimum_payment is not None
        assert program.minimum_payment.amount == Decimal(minimum)
        assert program.minimum_payment.currency == "BYN"
        assert program.maximum_reward is not None
        assert program.maximum_reward.amount == Decimal(maximum)
        assert program.maximum_reward.unit == unit
    yarkaya = cards["yarkaya_karta"].reward_programs[0]
    assert yarkaya.minimum_payment is not None
    assert yarkaya.minimum_payment.amount == Decimal("10")
    assert yarkaya.maximum_reward is not None and yarkaya.maximum_reward.unlimited
    cashalot = cards["belgazprombank_cashalot"].reward_programs[0]
    assert cashalot.minimum_payment is not None
    assert cashalot.minimum_payment.amount == Decimal("10")
    assert cashalot.maximum_reward is not None and cashalot.maximum_reward.unlimited

    rendered = format_limits(tuple(cards.values()))
    assert "💳 1-2-3 — 💵 мин. платёж 10 BYN · макс. в месяц 123 BYN" in rendered
    assert "💳 Социальная — 💵 мин. платёж 0 BYN · макс. в месяц 100 BYN" in rendered
    assert (
        "💳 Витамин Д — 💵 мин. платёж 10 BYN · макс. в месяц 50 BYN · "
        "⭐ мин. платёж 0 BYN · макс. в месяц 200 баллов"
    ) in rendered
    assert ("💳 КОМБОкарта — 💵 мин. платёж 5 BYN · макс. в месяц 130 BYN") in rendered
    oplati_line = next(line for line in rendered.splitlines() if "Оплати" in line)
    assert oplati_line.count("мин. платёж") == 1
    assert "месячный лимит не установлен" in oplati_line
    assert "мин. платёж 3 BYN" in oplati_line
    assert "лимит 20 BYN/7 дней" in oplati_line
    assert "операцию" not in oplati_line
    assert (
        "💳 Цептер Card — 💵 мин. платёж 0 BYN · макс. в месяц 150 BYN / 50 USD / 50 EUR"
    ) in rendered


def test_oplati_has_three_byn_minimum_and_only_a_weekly_reward_cap() -> None:
    catalog = _catalog()
    card = next(card for card in catalog.cards if card.id == "oplati")

    for program in card.reward_programs:
        assert program.minimum_payment is not None
        assert program.minimum_payment.amount == Decimal("3")
        assert program.minimum_payment.currency == "BYN"
        assert program.maximum_reward is None
        assert program.monthly_maximum_not_defined is True

    assert len(card.reward_limits) == 1
    cap = card.reward_limits[0]
    assert cap.amount == Decimal("20")
    assert cap.unit == "currency"
    assert cap.currency == "BYN"
    assert cap.period == "week"

    for mcc, percent in (("5411", "3"), ("4112", "1")):
        match = next(match for match in catalog.lookup(mcc) if match.card.id == "oplati")
        assert match.gross_percent == match.net_percent == Decimal(percent)


def test_real_catalog_expanded_results_show_effective_bank_and_reward_terms() -> None:
    catalog = _catalog()
    matches = catalog.lookup("5411")
    by_id = {match.card.id: match for match in matches}

    oplati = format_matches("5411", (by_id["oplati"],), _descriptions(), details=True)
    assert "💳 Оплати — 3%" in oplati
    assert "Белинвестбанк" in oplati
    assert "Мин. платёж 3 BYN" in oplati
    assert "20 BYN/неделю" in oplati
    assert "Банк:" not in oplati
    assert "операцию" not in oplati
    assert "20 BYN/мес." not in oplati

    vitamin = format_matches("5411", (by_id["vitamin_d"],), details=True)
    assert "Деньги:" in vitamin and "Баллы:" in vitamin
    assert "10 BYN" in vitamin and "без минимума" in vitamin
    assert "50 BYN/мес." in vitamin and "200 баллов/мес." in vitamin

    kufar = format_matches("5411", (by_id["kufar"],), details=True)
    assert "МТбанк" in kufar
    assert "Visa" not in kufar and "Kufar" not in kufar

    social = format_matches("5411", (by_id["mtbank_social"],), details=True)
    assert "100 BYN/мес." in social
    bnb = format_matches("5411", (by_id["bnb_1_2_3"],), details=True)
    assert "10 BYN" in bnb and "123 BYN/мес." in bnb

    pages = format_match_pages("5411", matches, _descriptions())
    assert len(pages) == 1
    assert pages[0].compact == format_matches("5411", matches, _descriptions())
    assert pages[0].expanded == format_matches("5411", matches, _descriptions(), details=True)
    assert len(pages[0].expanded.encode("utf-16-le")) // 2 <= 3900


def test_vitamin_points_only_details_do_not_show_cash_thresholds_or_caps() -> None:
    match = next(match for match in _catalog().lookup("1234") if match.card.id == "vitamin_d")
    rendered = format_matches("1234", (match,), details=True)
    assert "3% баллами" in rendered
    assert "200 баллов/мес." in rendered
    assert "без минимума" in rendered.casefold()
    assert "50 BYN" not in rendered and "10 BYN" not in rendered
    assert "Деньги:" not in rendered


def test_mcc_descriptions_are_pinned_shape_with_fallback() -> None:
    descriptions = _descriptions()
    assert descriptions.get("5411") == "Продуктовые магазины"
    assert descriptions.get("0742")
    assert descriptions.get("0000") == "описание не найдено"
    assert descriptions.get("5661") == "Обувь"
    assert descriptions.get("5309") == "Магазины беспошлинной торговли"
    assert descriptions.get("7297") == "Массаж"
    assert descriptions.get("7392") == (
        "Консультации по связям с общественностью"  # noqa: RUF001
    )
    assert all(
        re.search(r"[А-Яа-яЁё]", label)  # noqa: RUF001
        for label in descriptions.labels.values()
    )
