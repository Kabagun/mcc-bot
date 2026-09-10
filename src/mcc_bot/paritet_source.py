"""Collect the reviewed Paritetbank partner page into a static partner snapshot.

The Paritetbank page is server-rendered HTML.  It repeats the same partner tiles
under each category tab, so this module deliberately takes records only from
the ``#all`` panel and validates (rather than merges) copies found elsewhere.
The collector is a bounded source adapter: it never writes the bot database or
tries to infer cards that are not present in the local catalog.
"""

# ruff: noqa: RUF001
# Russian source labels intentionally sit next to Latin identifiers and URLs.
# RUF001 cannot distinguish those source strings from accidental homoglyphs.

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_SOURCE_URL = "https://www.paritetbank.by/private/partners/"
DEFAULT_MAX_HTML_BYTES = 10 * 1024 * 1024
_BITRIX_ID_RE = re.compile(r"^bx_(?P<prefix>\d+)_(?P<id>\d+)$")
_PERCENT_RE = re.compile(r"(?<![\d.,])(?P<value>\d+(?:[.,]\d+)?)\s*%")
_SPACE_RE = re.compile(r"\s+")
_ONLINE_RE = re.compile(
    r"\b(?:онлайн|online|интернет|на\s+сайт\w*|сайт\w*|прилож\w*)\b",
    re.I,
)
_OFFLINE_RE = re.compile(
    r"\b(?:офлайн|offline|терминал(?:ы|ах|ом)?|касс(?:а|е|ы|ах)|"
    r"магазин(?:е|ах)?|торгов(?:ая|ом|ые)|точк(?:а|и|ах)|"
    r"адрес(?:ам|ах)?|фитнес-центр)\b",
    re.I,
)
# These are the two page records whose terms cannot be faithfully represented
# by one ordinary partner offer.  The IDs are part of the source identity, so
# the name check below is only a defensive guard for a changed page.
HELD_SOURCE_IDS = frozenset({104700, 111408})

# The source's five card columns include ``# КОМБОкарта и карта Life`` first.
# Only that first row is normalized to the one existing local card ID.
PARITET_CARD_ID = "paritet_combo"


class ParitetSourceError(ValueError):
    """Raised when the source cannot be safely normalized."""


@dataclass(frozen=True)
class _Node:
    """Small parsed HTML node used instead of a broad third-party parser."""

    tag: str
    attrs: tuple[tuple[str, str], ...]
    children: tuple[object, ...]

    def attr(self, name: str) -> str | None:
        wanted = name.casefold()
        for key, value in self.attrs:
            if key.casefold() == wanted:
                return value
        return None

    def has_class(self, class_name: str) -> bool:
        classes = (self.attr("class") or "").split()
        return class_name in classes


class _TreeBuilder(HTMLParser):
    """Build a tolerant, comments-free tree while preserving source order."""

    _VOID_TAGS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _MutableNode("#document", {}, [])
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_attrs = {key.casefold(): value or "" for key, value in attrs}
        node = _MutableNode(tag.casefold(), normalized_attrs, [])
        self.stack[-1].children.append(node)
        if node.tag not in self._VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_attrs = {key.casefold(): value or "" for key, value in attrs}
        self.stack[-1].children.append(_MutableNode(tag.casefold(), normalized_attrs, []))

    def handle_endtag(self, tag: str) -> None:
        wanted = tag.casefold()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == wanted:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)

    def handle_comment(self, _data: str) -> None:
        # ORO has an old, commented-out rate block.  Comments are not source
        # evidence and must never win over the visible popup.
        return


@dataclass
class _MutableNode:
    tag: str
    attrs: dict[str, str]
    children: list[object]

    def attr(self, name: str) -> str | None:
        return self.attrs.get(name.casefold())

    def has_class(self, class_name: str) -> bool:
        return class_name in (self.attr("class") or "").split()

    def freeze(self) -> _Node:
        frozen_children = tuple(
            child.freeze() if isinstance(child, _MutableNode) else child for child in self.children
        )
        return _Node(self.tag, tuple(sorted(self.attrs.items())), frozen_children)


@dataclass(frozen=True)
class ParitetRecord:
    """One normalized source record before it becomes a partner offer."""

    source_id: int
    name: str
    rate: str
    channel: str
    conditions: str
    source_url: str
    fingerprint: str

    @property
    def source_key(self) -> str:
        """Return the stable source key used by the partner importer."""

        return f"paritet:{self.source_id}:{PARITET_CARD_ID}:{self.channel}"

    @property
    def brand_key(self) -> str:
        """Return one stable brand identity per numeric source record."""

        return f"brand:paritet:{self.source_id}"

    def as_offer(self) -> dict[str, object]:
        """Return the existing v1 partner-seed offer shape."""

        return {
            "source_key": self.source_key,
            "brand_key": self.brand_key,
            "brand": self.name,
            "aliases": [],
            "card_id": PARITET_CARD_ID,
            "channel": self.channel,
            "mode": "total",
            "reward_kind": "cash",
            "starts_on": None,
            "ends_on": None,
            "conditions": self.conditions,
            "source_url": self.source_url,
            "require_existing_mcc": False,
            "tiers": [{"value": self.rate}],
        }


@dataclass(frozen=True)
class ParitetCollection:
    """Deterministic snapshot and report generated from one HTML response."""

    snapshot: dict[str, object]
    report: dict[str, object]


def _normalize_space(value: str) -> str:
    value = html_lib.unescape(value).replace("\xa0", " ")
    return _SPACE_RE.sub(" ", value).strip()


def _descendants(node: _Node, *, tag: str | None = None) -> Iterator[_Node]:
    for child in node.children:
        if not isinstance(child, _Node):
            continue
        if tag is None or child.tag == tag:
            yield child
        yield from _descendants(child, tag=tag)


def _find_by_id(root: _Node, value: str) -> list[_Node]:
    return [node for node in _descendants(root) if node.attr("id") == value]


def _text(node: _Node) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, _Node):
            if child.tag in {"script", "style", "noscript", "template"}:
                continue
            parts.append(_text(child))
        else:
            parts.append(str(child))
    return _normalize_space(" ".join(parts))


def _text_without(node: _Node, excluded: set[int]) -> str:
    """Flatten a node while dropping selected descendant object identities."""

    if id(node) in excluded:
        return ""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, _Node):
            parts.append(_text_without(child, excluded))
        else:
            parts.append(str(child))
    return _normalize_space(" ".join(parts))


def _first_class(root: _Node, classes: Sequence[str]) -> _Node | None:
    for class_name in classes:
        for node in _descendants(root):
            if node.has_class(class_name):
                return node
    return None


def _contains(node: _Node, needle: _Node) -> bool:
    return any(
        child is needle or (isinstance(child, _Node) and _contains(child, needle))
        for child in node.children
    )


def _item_id(item: _Node) -> tuple[int, str]:
    candidates: list[str] = []
    for node in (item, *_descendants(item)):
        value = node.attr("id")
        if value and _BITRIX_ID_RE.fullmatch(value):
            candidates.append(value)
    unique = tuple(dict.fromkeys(candidates))
    if len(unique) != 1:
        raise ParitetSourceError(
            "Каждый #all m-back__item должен содержать ровно один Bitrix ID bx_<prefix>_<id>"
        )
    match = _BITRIX_ID_RE.fullmatch(unique[0])
    assert match is not None
    return int(match.group("id")), match.group("prefix")


def _popup(item: _Node) -> _Node:
    popup = _first_class(item, ("back-popup__info", "back-popup"))
    return popup or item


def _rate_from_popup(popup: _Node) -> str:
    rows = [node for node in _descendants(popup, tag="li") if _PERCENT_RE.search(_text(node))]
    search_nodes: Iterable[_Node] = rows or (popup,)
    for node in search_nodes:
        match = _PERCENT_RE.search(_text(node))
        if match:
            return _normalize_rate(match.group("value"))
    raise ParitetSourceError("В записи Paritet не найдена первая ставка КОМБОкарты")


def _normalize_rate(value: str) -> str:
    try:
        rate = Decimal(value.replace(",", "."))
    except InvalidOperation as exc:
        raise ParitetSourceError(f"Некорректная ставка Paritet: {value!r}") from exc
    if not rate.is_finite() or rate < 0 or rate > 100:
        raise ParitetSourceError(f"Ставка Paritet вне диапазона 0..100: {value!r}")
    normalized = format(rate.normalize(), "f")
    return normalized


def _name(item: _Node, popup: _Node) -> str:
    candidates: list[str] = []
    for node in (
        _first_class(popup, ("back-popup__title",)),
        _first_class(item, ("m-back__title", "m-back__name")),
    ):
        if node is not None:
            value = _text(node)
            if value and value not in candidates:
                candidates.append(value)
    for node in (item, popup):
        for attr in ("data-name", "data-title", "title"):
            value = _normalize_space(node.attr(attr) or "")
            if value and value not in candidates:
                candidates.append(value)
    if not candidates:
        raise ParitetSourceError("В записи Paritet не найдено название партнёра")
    # Tile and popup titles are source mirrors; a changed page that disagrees
    # between them is unsafe to silently choose.
    folded = {value.casefold() for value in candidates}
    if len(folded) > 1:
        raise ParitetSourceError("Название партнёра в плитке и popup расходится")
    return candidates[0]


def _source_url(item: _Node, popup: _Node) -> str:
    for root in (popup, item):
        for anchor in _descendants(root, tag="a"):
            href = _normalize_space(anchor.attr("href") or "")
            if href and not href.startswith("#") and not href.lower().startswith("javascript:"):
                return href
    return ""


def _condition_text(item: _Node, popup: _Node, name: str, source_url: str) -> str:
    explicit = _first_class(item, ("m-back__condition", "back-popup__condition"))
    if explicit is not None:
        return _normalize_space(_text(explicit))
    rows = {id(node) for node in _descendants(popup, tag="li") if _PERCENT_RE.search(_text(node))}
    rows.update(
        id(node)
        for node in _descendants(popup)
        if node.has_class("back-popup__top") or node.tag == "a"
    )
    value = _text_without(popup, rows)
    for unwanted in (name, "Манибэк", source_url):
        if unwanted:
            value = value.replace(unwanted, " ")
    value = re.sub(r"https?://\S+", " ", value)
    return _normalize_space(value)


def _channel(_source_id: int, _name: str, full_text: str) -> str:
    """Classify the source condition into the bot's existing channels."""

    online = bool(_ONLINE_RE.search(full_text))
    offline = bool(_OFFLINE_RE.search(full_text))
    if online and offline:
        return "any"
    if online:
        return "online"
    return "offline"


def _record(item: _Node, *, source_url: str, bitrix_prefix: str) -> ParitetRecord:
    source_id, item_prefix = _item_id(item)
    if item_prefix != bitrix_prefix:
        raise ParitetSourceError("У записей Paritet изменился префикс Bitrix ID")
    popup = _popup(item)
    name = _name(item, popup)
    rate = _rate_from_popup(popup)
    partner_url = _source_url(item, popup) or source_url
    conditions = _condition_text(item, popup, name, partner_url)
    channel = _channel(source_id, name, conditions)
    fingerprint_payload = "\x1f".join((name, rate, channel, conditions, partner_url))
    fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
    return ParitetRecord(
        source_id=source_id,
        name=name,
        rate=rate,
        channel=channel,
        conditions=conditions,
        source_url=partner_url,
        fingerprint=fingerprint,
    )


def _coerce_html(source_html: str | bytes) -> str:
    if isinstance(source_html, bytes):
        try:
            return source_html.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParitetSourceError("Не удалось декодировать HTML Paritet") from exc
    if isinstance(source_html, str):
        return source_html
    raise ParitetSourceError("HTML источника Paritet должен быть строкой или bytes")


def _parse_tree(source_html: str | bytes) -> tuple[_Node, _Node, tuple[_Node, ...], str]:
    source_html = _coerce_html(source_html)
    if not isinstance(source_html, str) or not source_html.strip():
        raise ParitetSourceError("HTML источника Paritet пуст")
    parser = _TreeBuilder()
    try:
        parser.feed(source_html)
        parser.close()
    except Exception as exc:  # HTMLParser errors indicate source structure drift.
        raise ParitetSourceError("Не удалось разобрать HTML Paritet") from exc
    document = parser.root.freeze()
    all_panels = _find_by_id(document, "all")
    if len(all_panels) != 1:
        raise ParitetSourceError("В HTML Paritet должен быть ровно один раздел #all")
    all_panel = all_panels[0]
    all_items = tuple(node for node in _descendants(all_panel) if node.has_class("m-back__item"))
    if not all_items:
        raise ParitetSourceError("В разделе #all не найдены m-back__item")
    return document, all_panel, all_items, hashlib.sha256(source_html.encode("utf-8")).hexdigest()


def _validate_category_copies(
    document: _Node,
    all_panel: _Node,
    canonical: dict[int, ParitetRecord],
    *,
    source_url: str,
) -> int:
    external = tuple(
        node
        for node in _descendants(document)
        if node.has_class("m-back__item") and not _contains(all_panel, node)
    )
    copies = 0
    prefix = None
    if canonical:
        first_id = next(iter(canonical))
        # Re-read the prefix from a canonical item only for the external-copy
        # parser; all canonical records have already checked consistency.
        for node in _descendants(all_panel):
            value = node.attr("id") or ""
            match = _BITRIX_ID_RE.fullmatch(value)
            if match and int(match.group("id")) == first_id:
                prefix = match.group("prefix")
                break
    if prefix is None:
        raise ParitetSourceError("Не удалось определить префикс Bitrix ID")
    for item in external:
        record = _record(item, source_url=source_url, bitrix_prefix=prefix)
        if record.source_id not in canonical:
            raise ParitetSourceError(
                f"Категорийная копия содержит неизвестный Bitrix ID {record.source_id}"
            )
        expected = canonical[record.source_id]
        # Category copies often omit the popup's category, URL, or some of the
        # non-Combo rows.  Compare the fields that define the normalized offer;
        # a changed name, first rate, or channel is source drift and aborts.
        if (record.name, record.rate, record.channel) != (
            expected.name,
            expected.rate,
            expected.channel,
        ):
            raise ParitetSourceError(
                f"Категорийная копия Bitrix ID {record.source_id} расходится с #all"
            )
        if (
            record.source_url != source_url
            and expected.source_url != source_url
            and record.source_url != expected.source_url
        ):
            raise ParitetSourceError(
                f"Категорийная копия Bitrix ID {record.source_id} содержит другой URL"
            )
        copies += 1
    return copies


def collect_paritet(
    source_html: str | bytes,
    *,
    source_url: str = DEFAULT_SOURCE_URL,
) -> ParitetCollection:
    """Parse HTML and return a deterministic reviewed snapshot plus report.

    The input is a saved UTF-8 response body.  The function does not perform
    network access and never writes files; use :func:`fetch_paritet_html` and
    :func:`write_reviewed_snapshot` at the edges when needed.
    """

    source_url = _normalize_space(source_url)
    if not source_url:
        raise ParitetSourceError("Для снимка Paritet нужен URL источника")
    document, all_panel, all_items, source_sha256 = _parse_tree(source_html)
    records: dict[int, ParitetRecord] = {}
    bitrix_prefix: str | None = None
    for item in all_items:
        source_id, item_prefix = _item_id(item)
        if bitrix_prefix is None:
            bitrix_prefix = item_prefix
        record = _record(item, source_url=source_url, bitrix_prefix=bitrix_prefix)
        if source_id in records:
            raise ParitetSourceError(f"Дублирующийся Bitrix ID {source_id} внутри #all")
        records[source_id] = record
    assert bitrix_prefix is not None
    category_copies = _validate_category_copies(document, all_panel, records, source_url=source_url)

    problems: list[dict[str, object]] = []
    offers: list[dict[str, object]] = []
    for source_id in sorted(records):
        record = records[source_id]
        folded_name = record.name.casefold()
        if source_id in HELD_SOURCE_IDS or folded_name in {"oro", "адреналин"}:
            if source_id == 104700 or folded_name == "oro":
                problems.append(
                    {
                        "kind": "ambiguous_reward",
                        "source_key": record.source_key,
                        "source_id": source_id,
                        "brand": record.name,
                        "action": "hold",
                        "reason": (
                            "Плитка ORO и видимый popup содержат разные ставки; offer не создаётся."
                        ),
                        "observed_combo_rate": record.rate,
                    }
                )
                continue
            problems.append(
                {
                    "kind": "location_specific_rates",
                    "source_key": record.source_key,
                    "source_id": source_id,
                    "brand": record.name,
                    "action": "hold",
                    "reason": (
                        "У Adrenalin разные ставки по адресам и способам оплаты; "
                        "одна offer их исказит."
                    ),
                    "observed_combo_rate": record.rate,
                }
            )
            continue
        offers.append(record.as_offer())
    problems.sort(key=lambda problem: (int(problem["source_id"]), str(problem["kind"])))
    source = {
        "id": "paritetbank_partners",
        "url": source_url,
        "scope": "#all",
        "html_sha256": source_sha256,
    }
    snapshot: dict[str, object] = {
        "version": 1,
        "reviewed": True,
        "source": source,
        "offers": offers,
        "exclusions": [],
        "problems": problems,
    }
    report: dict[str, object] = {
        "version": 1,
        "status": "review_required" if problems else "ready",
        "source": source,
        "counts": {
            "source_items": len(records),
            "unique_source_ids": len(records),
            "offers_emitted": len(offers),
            "held_items": len(problems),
            "category_copies_ignored": category_copies,
            "duplicate_source_ids": 0,
        },
        "problems": problems,
        "validation": {
            "ok": True,
            "canonical_panel": "#all",
            "bitrix_prefix": bitrix_prefix,
            "card_id": PARITET_CARD_ID,
            "normalization": "first visible percentage row only",
        },
    }
    return ParitetCollection(snapshot=snapshot, report=report)


def parse_paritet_html(
    source_html: str | bytes,
    *,
    source_url: str = DEFAULT_SOURCE_URL,
) -> tuple[ParitetRecord, ...]:
    """Return canonical normalized records that are safe to import."""

    collection = collect_paritet(source_html, source_url=source_url)
    records: list[ParitetRecord] = []
    for offer in collection.snapshot["offers"]:  # type: ignore[index]
        source_key = str(offer["source_key"])
        source_id = int(source_key.split(":", 2)[1])
        records.append(
            ParitetRecord(
                source_id=source_id,
                name=str(offer["brand"]),
                rate=str(offer["tiers"][0]["value"]),  # type: ignore[index]
                channel=str(offer["channel"]),
                conditions=str(offer["conditions"]),
                source_url=str(offer["source_url"]),
                fingerprint="",
            )
        )
    # Held records are intentionally absent from offers.  This function
    # returns only records that can be represented by the existing card schema.
    return tuple(sorted(records, key=lambda record: record.source_id))


def build_snapshot(
    source_html: str | bytes, *, source_url: str = DEFAULT_SOURCE_URL
) -> dict[str, object]:
    """Build and return only the reviewed v1 snapshot JSON object."""

    return collect_paritet(source_html, source_url=source_url).snapshot


def fetch_paritet_html(
    url: str = DEFAULT_SOURCE_URL,
    *,
    timeout: float = 30.0,
    max_bytes: int = DEFAULT_MAX_HTML_BYTES,
) -> str:
    """Fetch one bounded public HTML response for an explicit collection run."""

    if not url.startswith(("https://", "http://")):
        raise ParitetSourceError("URL Paritet должен начинаться с http:// или https://")
    if max_bytes <= 0:
        raise ParitetSourceError("Ограничение размера HTML Paritet должно быть положительным")
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "mcc-bot-paritet-review/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status < 200 or status >= 300:
                raise ParitetSourceError(f"Источник Paritet вернул HTTP {status}")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ParitetSourceError("HTML Paritet превышает ограниченный размер ответа")
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as exc:
        raise ParitetSourceError(f"Источник Paritet вернул HTTP {exc.code}") from exc
    except URLError as exc:
        raise ParitetSourceError(f"Не удалось получить HTML Paritet: {exc.reason}") from exc
    try:
        return body.decode(charset)
    except (LookupError, UnicodeDecodeError) as exc:
        raise ParitetSourceError("Не удалось декодировать HTML Paritet") from exc


def _read_input(value: Path | str | bytes) -> str:
    if isinstance(value, bytes):
        return _coerce_html(value)
    if isinstance(value, Path):
        try:
            return value.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ParitetSourceError(f"Не удалось прочитать HTML Paritet: {value}") from exc
    if "<" in value:
        return value
    return _read_input(Path(value))


def _json_text(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_reviewed_snapshot(
    source_html: str | bytes | Path,
    output_path: Path,
    *,
    report_path: Path | None = None,
    source_url: str = DEFAULT_SOURCE_URL,
) -> ParitetCollection:
    """Parse input and write deterministic snapshot/report only after validation."""

    html = _read_input(source_html)
    collection = collect_paritet(html, source_url=source_url)
    output_path = Path(output_path)
    if report_path is None:
        report_path = output_path.with_name(f"{output_path.stem}.report.json")
    if output_path.resolve() == Path(report_path).resolve():
        raise ParitetSourceError("Пути snapshot и report должны различаться")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_json_text(collection.snapshot), encoding="utf-8", newline="\n")
    report_path.write_text(_json_text(collection.report), encoding="utf-8", newline="\n")
    return collection


def main(argv: list[str] | None = None) -> int:
    """Run the explicit local Paritet snapshot command."""

    parser = argparse.ArgumentParser(description=__doc__)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--html", "--source-html", "--input-html", type=Path)
    input_group.add_argument("--url", type=str)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--source-url", type=str, default=None)
    args = parser.parse_args(argv)
    try:
        if args.html is not None:
            source_html: str | Path = args.html
            source_url = args.source_url or DEFAULT_SOURCE_URL
        else:
            source_html = fetch_paritet_html(args.url)
            source_url = args.source_url or args.url
        collection = write_reviewed_snapshot(
            source_html,
            args.output,
            report_path=args.report,
            source_url=source_url,
        )
    except ParitetSourceError as exc:
        print(f"paritet: {exc}", file=sys.stderr)
        return 2
    counts = collection.report["counts"]
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
