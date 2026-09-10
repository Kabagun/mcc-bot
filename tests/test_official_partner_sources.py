from __future__ import annotations

# ruff: noqa: RUF001
# Fixture assertions intentionally contain Russian source labels.
import json
from pathlib import Path

import pytest

import mcc_bot.official_partner_sources as official_sources
from mcc_bot.official_partner_sources import (
    DEFAULT_CACTUS_URL,
    OfficialPartnerSourceError,
    collect_bnb,
    collect_cactus,
    collect_cactus_pages,
    collect_izi,
    collect_statuscard,
    fetch_cactus,
    write_collection,
)

FIXTURES = Path(__file__).parent / "fixtures" / "official_partner_sources"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_cactus_fetch_skips_page_one_alias_and_uses_image_identity() -> None:
    page_two_url = f"{DEFAULT_CACTUS_URL}?cpage=page-2"
    pages = {
        DEFAULT_CACTUS_URL: _html("cactus-page-1.html"),
        page_two_url: _html("cactus-page-2.html"),
    }
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return pages[url]

    collection = fetch_cactus(fetcher=fetch)

    assert calls == [DEFAULT_CACTUS_URL, page_two_url]
    assert collection.report["counts"] == {
        "source_pages": 2,
        "source_items": 4,
        "unique_source_ids": 3,
        "offers_emitted": 3,
        "held_items": 0,
        "duplicate_source_ids": 1,
        "pagination_links": 2,
    }
    assert {offer["brand"] for offer in collection.snapshot["offers"]} == {
        'Сеть магазинов "Соседи"',
        'Сеть магазинов "ОМА"',
        "Без стабильной ссылки",
    }
    assert collection.snapshot["problems"] == []
    assert not any("page-1" in page for page in collection.snapshot["source"]["pages"])
    page_hashes = collection.snapshot["source"]["page_sha256"]
    assert set(page_hashes) == {DEFAULT_CACTUS_URL, page_two_url}
    assert all(len(value) == 64 for value in page_hashes.values())


def test_cactus_collection_is_deterministic_for_supplied_pages() -> None:
    page_one_url = f"{DEFAULT_CACTUS_URL}?cpage=page-1"
    page_two_url = f"{DEFAULT_CACTUS_URL}?cpage=page-2"
    pages = {
        DEFAULT_CACTUS_URL: _html("cactus-page-1.html"),
        page_one_url: _html("cactus-page-1.html"),
        page_two_url: _html("cactus-page-2.html"),
    }

    first = collect_cactus_pages(pages)
    second = collect_cactus_pages(dict(reversed(list(pages.items()))))

    assert first == second


def test_cactus_exact_name_identity_is_used_without_url_or_image() -> None:
    source = """
    <section class="grid-s"><div class="grid-s-wrap">
      <div class="about-banners__item about-banners__item--small">
        <a class="subpage-banner__link" href="#"></a>
        <h3 class="subpage-banner__title">Без identity</h3>
        <p class="subpage-banner__text">Возврат 2%</p>
      </div>
    </div></section>
    """

    collection = collect_cactus(source)

    assert len(collection.snapshot["offers"]) == 1
    assert collection.snapshot["offers"][0]["source_key"].startswith("cactus:name:")
    assert collection.snapshot["problems"] == []


def test_cactus_conflicting_image_identity_is_held() -> None:
    source = """
    <section class="grid-s"><div class="grid-s-wrap">
      <div class="about-banners__item about-banners__item--small">
        <a class="subpage-banner__link" href="#"></a>
        <img data-original="/upload/iblock/test/shared.webp">
        <h3 class="subpage-banner__title">Один партнер</h3>
        <p class="subpage-banner__text">Возврат 2%</p>
      </div>
      <div class="about-banners__item about-banners__item--small">
        <a class="subpage-banner__link" href="#"></a>
        <img data-original="/upload/iblock/test/shared.webp">
        <h3 class="subpage-banner__title">Другой партнер</h3>
        <p class="subpage-banner__text">Возврат 3%</p>
      </div>
    </div></section>
    """

    collection = collect_cactus(source)

    assert collection.snapshot["offers"] == []
    assert collection.snapshot["problems"][0]["kind"] == "conflicting_duplicate"


def test_cactus_cross_identity_duplicate_prefers_url_identity() -> None:
    page_two_url = f"{DEFAULT_CACTUS_URL}?cpage=page-2"
    collection = collect_cactus_pages(
        {
            DEFAULT_CACTUS_URL: _html("cactus-page-1.html"),
            page_two_url: _html("cactus-cross-identity.html"),
        }
    )

    assert collection.report["counts"]["source_items"] == 4
    assert collection.report["counts"]["unique_source_ids"] == 3
    assert collection.report["counts"]["duplicate_source_ids"] == 1
    assert collection.snapshot["problems"] == []
    offers = collection.snapshot["offers"]
    assert sum("prosvet" in offer["brand"].casefold() for offer in offers) == 1
    prosvet = next(offer for offer in offers if "prosvet" in offer["brand"].casefold())
    assert prosvet["source_key"].startswith("cactus:url:")
    assert prosvet["source_url"] == page_two_url


def test_cactus_cross_identity_rate_conflict_holds_all_rows() -> None:
    collection = collect_cactus(_html("cactus-conflicting-name.html"))

    assert collection.snapshot["offers"] == []
    assert collection.report["counts"]["source_items"] == 2
    assert collection.report["counts"]["unique_source_ids"] == 0
    assert collection.report["counts"]["held_items"] == 1
    problem = collection.snapshot["problems"][0]
    assert problem["kind"] == "conflicting_name"
    assert problem["observed_rates"] == ["2", "3"]
    assert problem["observed_channels"] == ["any"]


def test_cactus_alphanumeric_name_variants_deduplicate_without_transliteration() -> None:
    source = """
    <section class="grid-s"><div class="grid-s-wrap">
      <div class="about-banners__item about-banners__item--small">
        <a class="subpage-banner__link" href="#"></a>
        <h3 class="subpage-banner__title">Армтек</h3>
        <p class="subpage-banner__text">Возврат 2%</p>
      </div>
      <div class="about-banners__item about-banners__item--small">
        <a class="subpage-banner__link" href="#"></a>
        <h3 class="subpage-banner__title">Арм Тек</h3>
        <p class="subpage-banner__text">Возврат 2%</p>
      </div>
    </div></section>
    """

    collection = collect_cactus(source)

    assert collection.report["counts"] == {
        "source_pages": 1,
        "source_items": 2,
        "unique_source_ids": 1,
        "offers_emitted": 1,
        "held_items": 0,
        "duplicate_source_ids": 1,
        "pagination_links": 0,
    }
    assert collection.snapshot["problems"] == []


def test_bnb_emits_partner_component_and_holds_category_ambiguity() -> None:
    collection = collect_bnb(_html("bnb.html"))

    assert collection.report["counts"] == {
        "source_items": 2,
        "unique_source_ids": 2,
        "offers_emitted": 1,
        "held_items": 1,
        "duplicate_source_ids": 1,
        "popup_definitions": 2,
    }
    offer = collection.snapshot["offers"][0]
    assert offer["brand"] == "Соседи"
    assert offer["card_id"] == "bnb_1_2_3"
    assert offer["mode"] == "total"
    assert offer["reward_kind"] == "cash"
    assert offer["channel"] == "online"
    assert offer["tiers"] == [{"value": "2"}]
    assert "не следует повторно складывать" in offer["conditions"]
    assert collection.snapshot["problems"][0]["kind"] == "ambiguous_reward"


def test_bnb_equal_online_and_offline_rates_collapse_to_one_any_offer() -> None:
    collection = collect_bnb(_html("bnb-elema.html"))

    assert collection.report["counts"] == {
        "source_items": 1,
        "unique_source_ids": 1,
        "offers_emitted": 1,
        "held_items": 0,
        "duplicate_source_ids": 0,
        "popup_definitions": 1,
    }
    offer = collection.snapshot["offers"][0]
    assert offer["source_key"] == "bnb:285593:any"
    assert offer["brand"] == "ELEMA – бренд женской одежды ETELIER – бренд мужской одежды"
    assert offer["channel"] == "any"
    assert offer["tiers"] == [{"value": "2"}]
    assert collection.snapshot["problems"] == []


def test_izi_uses_exact_detail_row_and_deduplicates_repeated_map_marker() -> None:
    detail_url = "https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/1/"
    collection = collect_izi(
        _html("izi-list.html"),
        detail_pages={detail_url: _html("izi-detail.html")},
    )

    assert collection.report["counts"] == {
        "source_items": 2,
        "unique_source_ids": 1,
        "detail_pages": 1,
        "offers_emitted": 1,
        "held_items": 0,
        "duplicate_source_ids": 1,
    }
    offer = collection.snapshot["offers"][0]
    assert offer["brand"] == "Магазин Точный"
    assert offer["tiers"] == [{"value": "2"}]
    assert 'ООО "Точная организация"' in offer["conditions"]
    assert "MCC: 5411" in offer["conditions"]
    assert offer["mode"] == "total"
    assert "список сокращён" not in offer["conditions"]
    preview = official_sources._izi_public_preview(
        brand=offer["brand"], value=offer["tiers"][0]["value"], conditions=offer["conditions"]
    )
    assert (
        official_sources._telegram_length(preview) <= official_sources.TELEGRAM_SAFE_MESSAGE_LENGTH
    )
    assert len(collection.snapshot["source"]["detail_sha256"][detail_url]) == 64


def _izi_group_source() -> tuple[str, str]:
    source = """
    <script>
    var locations = [
      {'id':'77','coords':['53.9','27.5'],
       'name':'<div><p>ООО &quot;Группа&quot;</p><p>Сервис Группы</p><p>г. Минск, ул. Первая, 1</p>
       <a href="/fizicheskim_licam/cards/bonusy/izi/77/">Перейти</a></div>'},
      {'id':'77','coords':['53.8','27.6'],
       'name':'<div><p>ООО &quot;Группа&quot;</p><p>Сервис Группы</p><p>г. Минск, ул. Вторая, 2</p>
       <a href="/fizicheskim_licam/cards/bonusy/izi/77/">Перейти</a></div>'}
    ];
    </script>
    """
    detail = """
    <div class="breadcrumbs__item">
      <span class="breadcrumbs__link">ООО &quot;Группа&quot;</span>
    </div>
    <table><thead><tr>
      <th>Адрес</th><th>Наименование организации (сервиса)</th>
      <th>MCC</th><th>Кэш бэк</th>
    </tr></thead>
      <tbody>
        <tr><td>г. Минск, ул. Первая, 1</td><td>Сервис Группы</td><td>5411</td><td>2,0</td></tr>
        <tr><td>г. Минск, ул. Вторая, 2</td><td>Сервис Группы</td><td>5812</td><td>2,0</td></tr>
        <tr><td>г. Минск, ул. Первая, 1</td><td>Сервис Группы</td><td>5411</td><td>2,0</td></tr>
      </tbody>
    </table>
    """
    return source, detail


def test_izi_groups_same_brand_key_and_summarizes_outlets() -> None:
    source, detail = _izi_group_source()
    detail_url = "https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/77/"

    collection = collect_izi(source, detail_pages={detail_url: detail})

    assert len(collection.snapshot["offers"]) == 1
    assert collection.snapshot["problems"] == []
    offer = collection.snapshot["offers"][0]
    assert offer["brand_key"].startswith("brand:izi:77:")
    assert offer["source_key"].startswith("izi:77:brand:")
    assert "официальных точек: 2" in offer["conditions"]
    assert "г. Минск, ул. Вторая, 2; г. Минск, ул. Первая, 1" in offer["conditions"]
    assert "MCC: 5411, 5812" in offer["conditions"]
    assert offer["channel"] == "offline"
    assert offer["source_url"] == detail_url
    assert "г. Минск, ул. Первая, 1" in offer["conditions"]
    assert "г. Минск, ул. Вторая, 2" in offer["conditions"]
    preview = official_sources._izi_public_preview(
        brand=offer["brand"], value=offer["tiers"][0]["value"], conditions=offer["conditions"]
    )
    assert (
        official_sources._telegram_length(preview) <= official_sources.TELEGRAM_SAFE_MESSAGE_LENGTH
    )


def test_izi_quote_variants_merge_with_unquoted_display_label() -> None:
    detail_url = "https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/88/"
    collection = collect_izi(
        _html("izi-quote-list.html"),
        detail_pages={detail_url: _html("izi-quote-detail.html")},
    )

    assert len(collection.snapshot["offers"]) == 1
    assert collection.snapshot["problems"] == []
    offer = collection.snapshot["offers"][0]
    assert offer["brand"] == "Магазин Кулинария"
    assert offer["brand_key"].startswith("brand:izi:88:")
    assert "официальных точек: 2" in offer["conditions"]
    assert "г. Минск, ул. Вторая, 2; г. Минск, ул. Первая, 1" in offer["conditions"]
    assert "MCC: 5411, 5812" in offer["conditions"]


def test_izi_quote_variants_with_conflicting_rate_hold_the_group() -> None:
    detail_url = "https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/88/"
    collection = collect_izi(
        _html("izi-quote-list.html"),
        detail_pages={detail_url: _html("izi-quote-conflict-detail.html")},
    )

    assert collection.snapshot["offers"] == []
    assert len(collection.snapshot["problems"]) == 1
    problem = collection.snapshot["problems"][0]
    assert problem["kind"] == "conflicting_brand_offer"
    assert problem["observed_rates"] == ["2", "3"]
    assert problem["brand"] == "Магазин Кулинария"


def test_izi_internet_store_marker_is_online_even_with_a_physical_address() -> None:
    source, detail = _izi_group_source()
    detail_url = "https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/77/"
    source = source.replace("Сервис Группы", "Интернет-магазин -")
    detail = detail.replace("Сервис Группы", "Интернет-магазин -")

    collection = collect_izi(source, detail_pages={detail_url: detail})

    assert len(collection.snapshot["offers"]) == 1
    assert collection.snapshot["offers"][0]["channel"] == "online"
    assert collection.snapshot["problems"] == []


def test_izi_unknown_channel_is_held_instead_of_widened_to_any() -> None:
    source, detail = _izi_group_source()
    detail_url = "https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/77/"
    detail = detail.replace("г. Минск, ул. Вторая, 2", "—")

    collection = collect_izi(source, detail_pages={detail_url: detail})

    assert collection.snapshot["offers"] == []
    unknown = [
        item for item in collection.snapshot["problems"] if item["kind"] == "unknown_channel"
    ]
    assert len(unknown) == 1
    assert unknown[0]["observed_channels"] == ["offline", "unknown"]


def test_izi_same_store_label_across_entities_appends_exact_legal_entity() -> None:
    source = """
    <script>
    var locations = [
      {'id':'77','coords':['53.9','27.5'],
       'name':'<div><p>ООО &quot;Организация А&quot;</p><p>Магазин Общий</p>
       <p>г. Минск, ул. Первая, 1</p>
       <a href="/fizicheskim_licam/cards/bonusy/izi/77/">Перейти</a></div>'},
      {'id':'88','coords':['53.8','27.6'],
       'name':'<div><p>ООО &quot;Организация Б&quot;</p><p>Магазин-Общий</p>
       <p>г. Минск, ул. Вторая, 2</p>
       <a href="/fizicheskim_licam/cards/bonusy/izi/88/">Перейти</a></div>'}
    ];
    </script>
    """
    detail_template = """
    <div class="breadcrumbs__item"><span class="breadcrumbs__link">{entity}</span></div>
    <table><thead><tr>
      <th>Адрес</th><th>Наименование организации (сервиса)</th><th>MCC</th><th>Кэш бэк</th>
    </tr></thead><tbody>
      <tr><td>{address}</td><td>{store}</td><td>5411</td><td>2,0</td></tr>
    </tbody></table>
    """
    detail_77_url = "https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/77/"
    detail_88_url = "https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/88/"
    details = {
        detail_77_url: detail_template.format(
            entity='ООО "Организация А"', address="г. Минск, ул. Первая, 1", store="Магазин Общий"
        ),
        detail_88_url: detail_template.format(
            entity='ООО "Организация Б"', address="г. Минск, ул. Вторая, 2", store="Магазин-Общий"
        ),
    }

    collection = collect_izi(source, detail_pages=details)

    assert collection.snapshot["problems"] == []
    brands = {offer["brand"] for offer in collection.snapshot["offers"]}
    assert brands == {
        'Магазин Общий — ООО "Организация А"',
        'Магазин-Общий — ООО "Организация Б"',
    }


def test_izi_summarizes_addresses_only_when_render_budget_is_exceeded() -> None:
    address = "г. Минск, " + ("очень длинный адрес " * 300)
    source = f"""
    <script>
    var locations = [
      {{'id':'78','coords':['53.9','27.5'],
       'name':'<div><p>ООО &quot;Группа&quot;</p><p>Сервис Группы</p><p>{address}</p>
       <a href="/fizicheskim_licam/cards/bonusy/izi/78/">Перейти</a></div>'}}
    ];
    </script>
    """
    detail_url = "https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/78/"
    detail = f"""
    <div class="breadcrumbs__item">
      <span class="breadcrumbs__link">ООО &quot;Группа&quot;</span>
    </div>
    <table><thead><tr>
      <th>Адрес</th><th>Наименование организации (сервиса)</th><th>MCC</th><th>Кэш бэк</th>
    </tr></thead><tbody>
      <tr><td>{address}</td><td>Сервис Группы</td><td>5411</td><td>2,0</td></tr>
    </tbody></table>
    """

    collection = collect_izi(source, detail_pages={detail_url: detail})
    offer = collection.snapshot["offers"][0]

    assert "список сокращён для сообщения" in offer["conditions"]
    preview = official_sources._izi_public_preview(
        brand=offer["brand"], value=offer["tiers"][0]["value"], conditions=offer["conditions"]
    )
    assert (
        official_sources._telegram_length(preview) <= official_sources.TELEGRAM_SAFE_MESSAGE_LENGTH
    )


def test_izi_conflicting_rates_for_one_brand_key_are_held() -> None:
    source, detail = _izi_group_source()
    detail = detail.replace("<td>2,0</td>", "<td>3,0</td>", 1)
    detail_url = "https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/77/"

    collection = collect_izi(source, detail_pages={detail_url: detail})

    assert collection.snapshot["offers"] == []
    assert len(collection.snapshot["problems"]) == 1
    problem = collection.snapshot["problems"][0]
    assert problem["kind"] == "conflicting_brand_offer"
    assert problem["source_key"].startswith("brand:izi:77:")
    assert problem["observed_rates"] == ["2", "3"]


def test_izi_conflicting_channels_for_one_brand_key_are_held(monkeypatch) -> None:
    source, _detail = _izi_group_source()
    detail_url = "https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/77/"
    entity = 'ООО "Группа"'
    rows = [
        {
            "address": "г. Минск, ул. Первая, 1",
            "store": "Сервис Группы",
            "mcc": "5411",
            "rate": "2",
            "entity": entity,
            "channel": "online",
        },
        {
            "address": "г. Минск, ул. Вторая, 2",
            "store": "Сервис Группы",
            "mcc": "5812",
            "rate": "2",
            "entity": entity,
            "channel": "offline",
        },
    ]

    monkeypatch.setattr(official_sources, "_izi_detail_rows", lambda _html, _url: (entity, rows))
    collection = collect_izi(source, detail_pages={detail_url: "unused"})

    assert collection.snapshot["offers"] == []
    assert collection.snapshot["problems"][0]["kind"] == "conflicting_brand_offer"
    assert collection.snapshot["problems"][0]["observed_channels"] == ["offline", "online"]


def test_statuscard_emits_maniback_and_holds_discount() -> None:
    detail_url = "https://stbank.by/local/ajax/our_partners_maney-back_detail.php?ELEMENT_ID=42"
    no_reward_detail_url = (
        "https://stbank.by/local/ajax/our_partners_maney-back_detail.php?ELEMENT_ID=204422"
    )
    collection = collect_statuscard(
        _html("status-list.html"),
        detail_pages={
            detail_url: _html("status-detail-42.html"),
            no_reward_detail_url: _html("status-detail-204422.html"),
        },
    )

    assert collection.report["counts"] == {
        "source_items": 3,
        "unique_source_ids": 2,
        "detail_pages": 2,
        "offers_emitted": 2,
        "held_items": 1,
        "duplicate_source_ids": 0,
    }
    offer = next(item for item in collection.snapshot["offers"] if item["brand"] == "Ваша Мебель")
    assert offer["brand"] == "Ваша Мебель"
    assert offer["tiers"] == [{"value": "5"}]
    assert offer["channel"] == "offline"
    assert collection.snapshot["problems"][0]["kind"] == "discount_not_cashback"
    no_reward_offer = next(
        item for item in collection.snapshot["offers"] if item["brand"] == "Школа вокала Фа Соль"
    )
    assert no_reward_offer["tiers"] == [{"value": "10"}]
    assert no_reward_offer["channel"] == "offline"
    source = collection.snapshot["source"]
    assert len(source["html_sha256"]) == 64
    assert set(source["detail_sha256"]) == {detail_url, no_reward_detail_url}
    assert all(len(value) == 64 for value in source["detail_sha256"].values())


def test_source_snapshots_and_reports_are_written_deterministically(tmp_path: Path) -> None:
    collection = collect_bnb(_html("bnb.html"))
    output = tmp_path / "bnb.json"
    report = tmp_path / "bnb.report.json"

    write_collection(collection, output, report_path=report)
    first_snapshot = output.read_text(encoding="utf-8")
    first_report = report.read_text(encoding="utf-8")
    write_collection(collection, output, report_path=report)

    assert output.read_text(encoding="utf-8") == first_snapshot
    assert report.read_text(encoding="utf-8") == first_report
    assert json.loads(first_snapshot)["version"] == 1
    assert json.loads(first_report)["status"] == "review_required"


@pytest.mark.parametrize(
    "collector, source",
    [
        (collect_cactus, "<html><body><div class='not-cactus'></div></body></html>"),
        (collect_bnb, "<html><body><a class='not-partner'></a></body></html>"),
    ],
)
def test_required_source_selector_drift_fails_closed(collector, source: str) -> None:
    with pytest.raises(OfficialPartnerSourceError):
        collector(source)


def test_bnb_missing_total_additional_semantics_fails_closed() -> None:
    source = _html("bnb.html").replace("дополнительный манибэк", "манибэк", 1)

    with pytest.raises(OfficialPartnerSourceError, match="semantics are missing"):
        collect_bnb(source)
