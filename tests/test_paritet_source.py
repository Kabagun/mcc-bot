from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcc_bot.paritet_source import (
    ParitetSourceError,
    collect_paritet,
    parse_paritet_html,
    write_reviewed_snapshot,
)

FIXTURE = Path(__file__).parent / "fixtures" / "paritet_partners.html"


def _html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_collects_only_unique_all_records_and_holds_unsafe_rows() -> None:
    collection = collect_paritet(_html())

    offers = collection.snapshot["offers"]
    assert [offer["source_key"] for offer in offers] == [
        "paritet:101:paritet_combo:offline",
        "paritet:102:paritet_combo:online",
        "paritet:103:paritet_combo:any",
    ]
    assert [offer["tiers"] for offer in offers] == [
        [{"value": "2.5"}],
        [{"value": "3"}],
        [{"value": "4"}],
    ]
    assert all(offer["card_id"] == "paritet_combo" for offer in offers)
    assert all(offer["mode"] == "total" and offer["reward_kind"] == "cash" for offer in offers)
    assert all(offer["require_existing_mcc"] is False for offer in offers)
    assert collection.report["counts"] == {
        "category_copies_ignored": 2,
        "duplicate_source_ids": 0,
        "held_items": 2,
        "offers_emitted": 3,
        "source_items": 5,
        "unique_source_ids": 5,
    }
    assert [problem["source_id"] for problem in collection.report["problems"]] == [104, 105]
    assert [problem["source_key"] for problem in collection.report["problems"]] == [
        "paritet:104:paritet_combo:offline",
        "paritet:105:paritet_combo:any",
    ]
    assert {problem["action"] for problem in collection.report["problems"]} == {"hold"}


def test_first_rate_comes_from_visible_popup_not_comment_or_other_columns() -> None:
    records = parse_paritet_html(_html())

    assert [(record.source_id, record.rate) for record in records] == [
        (101, "2.5"),
        (102, "3"),
        (103, "4"),
    ]
    assert records[0].conditions == "* при оплате через терминалы магазина"
    assert records[1].channel == "online"
    assert records[2].channel == "any"


@pytest.mark.parametrize(
    "condition",
    [
        "при оплате в приложении",
        "при оплате приложением",
        "при оплате на сайтах партнёров",
    ],
)
def test_channel_classifier_handles_russian_online_inflections(condition: str) -> None:
    source = f"""
    <div id="all">
      <div class="m-back__item">
        <div class="m-back__in" id="bx_647316830_900">
          <div class="back-popup"><div class="back-popup__info">
            <div class="back-popup__title">Inflected online partner</div>
            Манибэк
            <ul><li><b>2%</b> - при оплате # КОМБОкартой и картой Life в Paritetbank</li></ul>
            * {condition}
          </div></div>
        </div>
      </div>
    </div>
    """

    offer = collect_paritet(source).snapshot["offers"][0]
    assert offer["channel"] == "online"


def test_snapshot_and_report_are_deterministic_and_written_to_requested_paths(
    tmp_path: Path,
) -> None:
    output = tmp_path / "paritet.json"
    report = tmp_path / "review.json"

    first = write_reviewed_snapshot(FIXTURE, output, report_path=report)
    first_snapshot = output.read_text(encoding="utf-8")
    first_report = report.read_text(encoding="utf-8")
    second = write_reviewed_snapshot(FIXTURE, output, report_path=report)

    assert first.snapshot == second.snapshot
    assert first.report == second.report
    assert output.read_text(encoding="utf-8") == first_snapshot
    assert report.read_text(encoding="utf-8") == first_report
    assert json.loads(first_snapshot)["source"]["html_sha256"]


def test_duplicate_inside_all_fails_closed() -> None:
    source = _html().replace(
        '<div class="m-back__item">\n          <div class="m-back__in" id="bx_647316830_102">',
        '<div class="m-back__item">\n          <div class="m-back__in" id="bx_647316830_101">',
        1,
    )

    with pytest.raises(ParitetSourceError, match="Дублирующийся Bitrix ID"):
        collect_paritet(source)


def test_conflicting_category_copy_fails_closed() -> None:
    source = _html().replace(
        '<div class="back-popup__title">Онлайн магазин</div>\n            Манибэк',
        '<div class="back-popup__title">Изменённая копия</div>\n            Манибэк',
        1,
    )

    with pytest.raises(ParitetSourceError, match="Категорийная копия"):
        collect_paritet(source)


@pytest.mark.parametrize(
    "source, message",
    [
        (
            "<div id='not-all'><div class='m-back__item'></div></div>",
            "ровно один раздел #all",
        ),
        (
            "<div id='all'><div class='m-back__item'><div class='m-back__in' "
            "id='wrong'></div></div></div>",
            "ровно один Bitrix ID",
        ),
        (
            "<div id='all'><div class='m-back__item'><div class='m-back__in' "
            "id='bx_1_1'><div class='m-back__title'>X</div></div></div></div>",
            "первая ставка",
        ),
    ],
)
def test_source_structure_drift_fails_closed(source: str, message: str) -> None:
    with pytest.raises(ParitetSourceError, match=message):
        collect_paritet(source)
