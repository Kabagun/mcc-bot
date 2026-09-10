"""Read-only collectors for official card-partner catalogues.

The collectors in this module only turn an official response into the existing
version-1 partner-seed shape.  They do not touch the bot database and they do
not try to identify a local merchant by a similar name.  A network fetch is
available through the ``fetch_*`` functions; the ``collect_*`` functions take
saved response bodies so a review can be reproduced from local fixtures.

The source sites are deliberately parsed with small standard-library parsers.
That keeps the source contract visible and lets us fail closed when a required
selector or field disappears instead of emitting a plausible but unsafe row.
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
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

DEFAULT_CACTUS_URL = "https://www.mtbank.by/cards/cactus/part/"
DEFAULT_BNB_URL = "https://bnb.by/bonus/"
DEFAULT_IZI_URL = "https://belarusbank.by/fizicheskim_licam/cards/bonusy/izi/"
DEFAULT_STATUS_URL = "https://stbank.by/local/ajax/partners_maney-back.php"
DEFAULT_STATUS_DETAIL_URL = (
    "https://stbank.by/local/ajax/our_partners_maney-back_detail.php?ELEMENT_ID={id}"
)
DEFAULT_MAX_HTML_BYTES = 12 * 1024 * 1024
DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_DETAILS = 2_000

# Telegram counts message length in UTF-16 code units.  Keep rendered Izi
# conditions below the normal UI's conservative 3,900-unit budget so the
# public store card can still add its heading, reward line, and warnings.  This
# is a render budget, not a lossy source-field limit: current Izi pages fit with
# their complete address lists and are shortened only when the rendered card
# would exceed this bound.
TELEGRAM_MAX_MESSAGE_LENGTH = 4_096
TELEGRAM_SAFE_MESSAGE_LENGTH = 3_900

CACTUS_CARD_ID = "cactus_mtbank"
BNB_CARD_ID = "bnb_1_2_3"
IZI_CARD_ID = "belarusbank_izi"
STATUS_CARD_ID = "statusbank_statuskarta"

_SPACE_RE = re.compile(r"\s+")
_PERCENT_RE = re.compile(r"(?<![\d.,])(?P<value>\d+(?:[.,]\d+)?)\s*%")
_ID_RE = re.compile(r"(?P<id>\d+)")
_URL_SCHEME_RE = re.compile(r"^https?://", re.I)
_ONLINE_RE = re.compile(
    r"\b(?:онлайн|online|интернет(?:-магазин)?|на\s+сайт(?:е|у)?|сайт(?:е|у)?|"
    r"приложени(?:е|ю)|приложении)\b",
    re.I,
)
_OFFLINE_RE = re.compile(
    r"\b(?:офлайн|offline|магазин(?:е|ах)?|офис(?:е|ах)?|касс(?:а|е|ы|ах)?|"
    r"торгов(?:ая|ом|ые|ых)?\s+(?:точк|зал)|адрес|клиник(?:а|е|у)|"
    r"центр(?:е|ы)?|павильон(?:е|ы)?)\b",
    re.I,
)
_IZI_ONLINE_STORE_RE = re.compile(r"\bинтернет\s*-\s*магазин\b", re.I)
_MISSING_LOCATION_RE = re.compile(r"^(?:[-–—−]|нет|не указано|отсутствует)$", re.I)
_MCC_RE = re.compile(r"^\d{4}$")
_JS_FIELD_RE = re.compile(r"'(?P<field>id|coords|name)'\s*:")


class OfficialPartnerSourceError(ValueError):
    """Raised when a source cannot be safely normalized."""


@dataclass(frozen=True)
class PartnerSourceCollection:
    """One deterministic v1 snapshot and its privacy-safe report."""

    snapshot: dict[str, object]
    report: dict[str, object]


@dataclass(frozen=True)
class _Node:
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
        return class_name in (self.attr("class") or "").split()


@dataclass
class _MutableNode:
    tag: str
    attrs: dict[str, str]
    children: list[object]

    def freeze(self) -> _Node:
        return _Node(
            self.tag,
            tuple(sorted(self.attrs.items())),
            tuple(
                child.freeze() if isinstance(child, _MutableNode) else child
                for child in self.children
            ),
        )


class _TreeBuilder(HTMLParser):
    """Build a tolerant source-order tree without third-party dependencies."""

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
        node = _MutableNode(
            tag.casefold(),
            {key.casefold(): value or "" for key, value in attrs},
            [],
        )
        self.stack[-1].children.append(node)
        if node.tag not in self._VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(
            _MutableNode(
                tag.casefold(),
                {key.casefold(): value or "" for key, value in attrs},
                [],
            )
        )

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
        return


def _normalize_space(value: str) -> str:
    return _SPACE_RE.sub(" ", html_lib.unescape(value).replace("\xa0", " ")).strip()


def _parse_html(source_html: str, source_name: str) -> _Node:
    if not isinstance(source_html, str) or not source_html.strip():
        raise OfficialPartnerSourceError(f"{source_name}: empty HTML response")
    parser = _TreeBuilder()
    try:
        parser.feed(source_html)
        parser.close()
    except Exception as exc:  # pragma: no cover - HTMLParser is intentionally tolerant
        raise OfficialPartnerSourceError(f"{source_name}: HTML parsing failed") from exc
    return parser.root.freeze()


def _descendants(node: _Node, *, tag: str | None = None) -> Iterable[_Node]:
    for child in node.children:
        if not isinstance(child, _Node):
            continue
        if tag is None or child.tag == tag:
            yield child
        yield from _descendants(child, tag=tag)


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


def _find_class(node: _Node, class_name: str, *, tag: str | None = None) -> list[_Node]:
    return [item for item in _descendants(node, tag=tag) if item.has_class(class_name)]


def _first_class(node: _Node, class_name: str, *, tag: str | None = None) -> _Node | None:
    return next(iter(_find_class(node, class_name, tag=tag)), None)


def _parse_rate(value: str, *, source_name: str) -> str:
    match = _PERCENT_RE.search(value)
    if match is None:
        raise OfficialPartnerSourceError(f"{source_name}: reward percentage is missing")
    try:
        parsed = Decimal(match.group("value").replace(",", "."))
    except InvalidOperation as exc:
        raise OfficialPartnerSourceError(f"{source_name}: invalid reward percentage") from exc
    if not parsed.is_finite() or parsed < 0 or parsed > 100:
        raise OfficialPartnerSourceError(f"{source_name}: reward percentage is out of range")
    return format(parsed.normalize(), "f")


def _rates(value: str) -> tuple[str, ...]:
    result: list[str] = []
    for match in _PERCENT_RE.finditer(value):
        try:
            parsed = Decimal(match.group("value").replace(",", "."))
        except InvalidOperation:
            continue
        if parsed.is_finite() and 0 <= parsed <= 100:
            result.append(format(parsed.normalize(), "f"))
    return tuple(result)


def _normalize_rate_list(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda value: (Decimal(value), value)))


def _normalize_url(value: str, *, base_url: str | None = None) -> str:
    value = _normalize_space(value)
    # Fragment-only placeholders such as ``#`` and ``#popup`` are UI hooks,
    # not stable source URLs.  Resolve relative paths only after rejecting
    # those hooks so a tile cannot accidentally become the catalogue URL.
    if not value or value.startswith("#"):
        return ""
    if base_url:
        value = urljoin(base_url, value)
    parts = urlsplit(value)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return ""
    path = parts.path or "/"
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, parts.query, ""))


def _stable_hash(value: str, *, length: int = 20) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def _source_sha(source_html: str) -> str:
    return hashlib.sha256(source_html.encode("utf-8")).hexdigest()


def _telegram_length(value: str) -> int:
    """Return Telegram's conservative UTF-16 message length for ``value``."""

    return len(value.encode("utf-16-le")) // 2


def _json_text(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _build_collection(
    *,
    source: dict[str, object],
    offers: Sequence[dict[str, object]],
    exclusions: Sequence[dict[str, object]] = (),
    problems: Sequence[dict[str, object]] = (),
    counts: Mapping[str, int],
    validation: Mapping[str, object],
) -> PartnerSourceCollection:
    ordered_offers = sorted(offers, key=lambda item: str(item["source_key"]))
    ordered_exclusions = sorted(exclusions, key=lambda item: str(item["source_key"]))
    ordered_problems = sorted(
        (dict(problem) for problem in problems),
        key=lambda problem: (
            str(problem.get("kind", "")),
            str(problem.get("source_key", "")),
            str(problem.get("reason", "")),
        ),
    )
    status = "review_required" if ordered_problems else "ready"
    snapshot: dict[str, object] = {
        "version": 1,
        "reviewed": True,
        "source": dict(source),
        "offers": ordered_offers,
        "exclusions": ordered_exclusions,
        "problems": ordered_problems,
    }
    report: dict[str, object] = {
        "version": 1,
        "status": status,
        "source": dict(source),
        "counts": dict(counts),
        "problems": ordered_problems,
        "validation": dict(validation),
    }
    return PartnerSourceCollection(snapshot=snapshot, report=report)


def _offer(
    *,
    source_key: str,
    brand_key: str,
    brand: str,
    aliases: Sequence[str] = (),
    card_id: str,
    channel: str,
    mode: str,
    reward_kind: str,
    value: str,
    conditions: str,
    source_url: str,
    starts_on: date | None = None,
    ends_on: date | None = None,
    tier_extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    tier: dict[str, object] = {"value": value}
    if tier_extra:
        tier.update(tier_extra)
    return {
        "source_key": source_key,
        "brand_key": brand_key,
        "brand": _normalize_space(brand),
        "aliases": list(
            dict.fromkeys(_normalize_space(alias) for alias in aliases if _normalize_space(alias))
        ),
        "card_id": card_id,
        "channel": channel,
        "mode": mode,
        "reward_kind": reward_kind,
        "starts_on": starts_on.isoformat() if starts_on else None,
        "ends_on": ends_on.isoformat() if ends_on else None,
        "conditions": _normalize_space(conditions),
        "source_url": source_url,
        "require_existing_mcc": False,
        "tiers": [tier],
    }


def _problem(
    kind: str, reason: str, *, source_key: str | None = None, **extra: object
) -> dict[str, object]:
    result: dict[str, object] = {"kind": kind, "action": "hold", "reason": reason}
    if source_key is not None:
        result["source_key"] = source_key
    result.update(extra)
    return result


def _channel_from_text(value: str, *, default: str | None = None) -> str | None:
    online = bool(_ONLINE_RE.search(value))
    offline = bool(_OFFLINE_RE.search(value))
    if online and offline:
        return "any"
    if online:
        return "online"
    if offline:
        return "offline"
    return default


def _http_text(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str | None = None,
) -> str:
    if not _URL_SCHEME_RE.match(url):
        raise OfficialPartnerSourceError("source URL must start with http:// or https://")
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "User-Agent": "mcc-bot-official-partner-review/1.0",
    }
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(url, headers=headers, method=method, data=body)
    try:
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status < 200 or status >= 300:
                raise OfficialPartnerSourceError(f"source returned HTTP {status}")
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise OfficialPartnerSourceError(
                    "source response exceeds the configured size limit"
                )
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as exc:
        raise OfficialPartnerSourceError(f"source returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise OfficialPartnerSourceError(f"source request failed: {exc.reason}") from exc
    try:
        return raw.decode(charset)
    except (LookupError, UnicodeDecodeError) as exc:
        raise OfficialPartnerSourceError("source response could not be decoded") from exc


def _write_collection(
    collection: PartnerSourceCollection, output_path: Path, report_path: Path | None
) -> None:
    report_path = report_path or output_path.with_name(f"{output_path.stem}.report.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_json_text(collection.snapshot), encoding="utf-8", newline="\n")
    report_path.write_text(_json_text(collection.report), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# Cactus / MTBank


def _canonical_cactus_page_url(value: str, *, base_url: str | None = None) -> str:
    """Normalize the Cactus alias where ``cpage=page-1`` is the base page."""

    normalized = _normalize_url(value, base_url=base_url)
    if not normalized:
        return ""
    parts = urlsplit(normalized)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if len(query) == 1 and query[0][0].casefold() == "cpage" and query[0][1].casefold() == "page-1":
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
    return normalized


def _cactus_items(root: _Node) -> list[_Node]:
    return [
        node
        for node in _descendants(root, tag="div")
        if node.has_class("about-banners__item") and node.has_class("about-banners__item--small")
    ]


def _cactus_pagination(root: _Node, page_url: str) -> tuple[str, ...]:
    pagination = _find_class(root, "grid-s-pagination")
    if not pagination:
        return ()
    links: list[str] = []
    for node in _descendants(pagination[0], tag="a"):
        href = _canonical_cactus_page_url(node.attr("href") or "", base_url=page_url)
        if href:
            links.append(href)
    return tuple(dict.fromkeys(links))


def _cactus_image_identities(item: _Node, page_url: str) -> tuple[str, ...]:
    identities: list[str] = []
    for image in _descendants(item, tag="img"):
        raw_value = image.attr("data-original") or ""
        normalized = _normalize_url(raw_value, base_url=page_url)
        if not normalized:
            continue
        if not urlsplit(normalized).path.casefold().startswith("/upload/iblock/"):
            continue
        identities.append(normalized)
    return tuple(dict.fromkeys(identities))


def _parse_cactus_page(
    source_html: str, page_url: str
) -> tuple[list[dict[str, object]], list[dict[str, object]], tuple[str, ...]]:
    root = _parse_html(source_html, "Cactus")
    items = _cactus_items(root)
    if not items:
        raise OfficialPartnerSourceError(
            f"Cactus: required partner tile selector disappeared on {page_url}"
        )
    records: list[dict[str, object]] = []
    problems: list[dict[str, object]] = []
    for index, item in enumerate(items):
        title = _first_class(item, "subpage-banner__title")
        reward = _first_class(item, "subpage-banner__text")
        if title is None or reward is None:
            raise OfficialPartnerSourceError(
                f"Cactus: partner tile {index} lost title or reward selector"
            )
        name = _text(title)
        reward_text = _text(reward)
        if not name:
            raise OfficialPartnerSourceError(f"Cactus: partner tile {index} has an empty name")
        rates = _rates(reward_text)
        if len(rates) != 1:
            key = f"cactus:page:{_stable_hash(page_url)}:item:{index}"
            problems.append(
                _problem(
                    "ambiguous_reward" if rates else "missing_reward",
                    "Cactus tile must contain exactly one visible reward percentage.",
                    source_key=key,
                    brand=name,
                    observed_rates=list(rates),
                    source_url=page_url,
                )
            )
            continue
        anchor = _first_class(item, "subpage-banner__link", tag="a")
        partner_url = _normalize_url(anchor.attr("href") if anchor else "", base_url=page_url)
        image_identities = _cactus_image_identities(item, page_url)
        if not partner_url:
            if len(image_identities) > 1:
                key = f"cactus:page:{_stable_hash(page_url)}:item:{index}"
                problems.append(
                    _problem(
                        "conflicting_identity",
                        (
                            "Cactus tile has multiple distinct official image identities; "
                            "offer is held."
                        ),
                        source_key=key,
                        brand=name,
                        identities=list(image_identities),
                        source_url=page_url,
                    )
                )
                continue
            if image_identities:
                identity = image_identities[0]
                source_key = f"cactus:image:{_stable_hash(identity)}"
                identity_kind = "image"
            else:
                # A small number of official tiles intentionally have neither
                # an external link nor an image.  The exact normalized visible
                # name is the remaining source identity; duplicate handling
                # below still holds conflicting rows instead of guessing.
                identity = f"name:{_normalize_space(name).casefold()}"
                if identity == "name:":
                    key = f"cactus:page:{_stable_hash(page_url)}:item:{index}"
                    problems.append(
                        _problem(
                            "missing_partner_url",
                            (
                                "Cactus tile has no stable official partner URL, image, "
                                "or name identity; offer is held."
                            ),
                            source_key=key,
                            source_url=page_url,
                        )
                    )
                    continue
                source_key = f"cactus:name:{_stable_hash(identity)}"
                identity_kind = "name"
        else:
            identity = partner_url
            source_key = f"cactus:url:{_stable_hash(partner_url)}"
            identity_kind = "url"
        records.append(
            {
                "source_key": source_key,
                "brand_key": f"brand:cactus:{identity_kind}:{_stable_hash(identity)}",
                "brand": name,
                "identity_kind": identity_kind,
                "aliases": (),
                "rate": rates[0],
                "channel": _channel_from_text(reward_text, default="any") or "any",
                "conditions": (
                    "Возврат бонусными баллами по бесплатной функции «Партнёрская сеть»; "
                    f"официальная ставка источника: {reward_text}."
                ),
                "source_url": page_url,
                "partner_url": partner_url,
                "identity": identity,
            }
        )
    return records, problems, _cactus_pagination(root, page_url)


def _cactus_dedupe_name_identities(
    records_by_key: dict[str, dict[str, object]], problems: list[dict[str, object]]
) -> tuple[int, int]:
    """Dedupe exact normalized names after stable-identity parsing.

    Cactus occasionally publishes one tile with an external URL and another
    with only an image while changing capitalization in the visible name.  A
    same-name/same-rate/channel row is one source fact; retain the strongest
    identity (URL, then image, then name).  A same-name conflict in rate or
    channel is held as a group rather than choosing one advertisement.
    """

    by_name: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for source_key, record in records_by_key.items():
        # The source sometimes changes only spacing/punctuation (for example
        # ``Армтек`` versus ``Арм Тек``).  Keep Cyrillic ``ё`` distinct and do
        # not transliterate: this is an alphanumeric identity, not fuzzy
        # matching across scripts.
        name_key = _alphanumeric_label_key(str(record["brand"]))
        by_name.setdefault(name_key, []).append((source_key, record))

    identity_rank = {"url": 0, "image": 1, "name": 2}
    duplicate_source_ids = 0
    held_source_items = 0
    for name_key in sorted(by_name):
        group = sorted(by_name[name_key], key=lambda item: item[0])
        if len(group) < 2:
            continue
        signatures = {(str(record["rate"]), str(record["channel"])) for _, record in group}
        if len(signatures) > 1:
            held_source_items += len(group)
            for source_key, _record in group:
                records_by_key.pop(source_key, None)
            first_record = group[0][1]
            problems.append(
                _problem(
                    "conflicting_name",
                    (
                        "Cactus tiles have the same normalized partner name but conflicting "
                        "rates or payment channels; all rows are held."
                    ),
                    source_key=f"cactus:name:{_stable_hash(name_key)}",
                    brand=str(first_record["brand"]),
                    observed_rates=sorted({rate for rate, _channel in signatures}),
                    observed_channels=sorted({channel for _rate, channel in signatures}),
                    source_keys=[source_key for source_key, _record in group],
                )
            )
            continue
        winner_key, _winner = min(
            group,
            key=lambda item: (
                identity_rank.get(str(item[1].get("identity_kind")), 99),
                item[0],
            ),
        )
        for source_key, _record in group:
            if source_key == winner_key:
                continue
            records_by_key.pop(source_key, None)
            duplicate_source_ids += 1
    return duplicate_source_ids, held_source_items


def collect_cactus_pages(
    pages: Mapping[str, str],
    *,
    source_url: str = DEFAULT_CACTUS_URL,
) -> PartnerSourceCollection:
    """Normalize all supplied Cactus pages and de-duplicate partner URLs."""

    source_url = _canonical_cactus_page_url(source_url)
    if not source_url:
        raise OfficialPartnerSourceError("Cactus: source URL is invalid")
    if not pages:
        raise OfficialPartnerSourceError("Cactus: no page responses supplied")
    page_entries: dict[str, list[tuple[str, str]]] = {}
    for key, value in pages.items():
        normalized_key = _normalize_url(key, base_url=source_url)
        canonical_key = _canonical_cactus_page_url(normalized_key, base_url=source_url)
        if not canonical_key:
            raise OfficialPartnerSourceError("Cactus: a supplied page URL is invalid")
        page_entries.setdefault(canonical_key, []).append((normalized_key, value))
    # Prefer an explicitly supplied base URL over its page-1 alias.  This also
    # makes a saved response map behave like the live fetcher, which skips the
    # duplicate alias after the base page has been seen.
    normalized_pages = {
        canonical_key: next(
            (value for raw_key, value in entries if raw_key == canonical_key), entries[0][1]
        )
        for canonical_key, entries in page_entries.items()
    }
    if "" in normalized_pages:
        raise OfficialPartnerSourceError("Cactus: a supplied page URL is invalid")
    if source_url not in normalized_pages:
        # A saved single-page fixture is allowed to use the explicit source URL
        # as its key; otherwise the missing first response is unsafe.
        first_url = next(iter(normalized_pages))
        if len(normalized_pages) == 1:
            normalized_pages[source_url] = normalized_pages.pop(first_url)
        else:
            raise OfficialPartnerSourceError("Cactus: first-page response is missing")

    pending = [source_url]
    seen: set[str] = set()
    records_by_key: dict[str, dict[str, object]] = {}
    problems: list[dict[str, object]] = []
    duplicate_source_ids = 0
    conflicting_keys: set[str] = set()
    pagination_links: set[str] = set()
    while pending:
        page_url = pending.pop(0)
        if page_url in seen:
            continue
        seen.add(page_url)
        if page_url not in normalized_pages:
            raise OfficialPartnerSourceError(f"Cactus: pagination response missing for {page_url}")
        records, page_problems, links = _parse_cactus_page(normalized_pages[page_url], page_url)
        problems.extend(page_problems)
        pagination_links.update(links)
        for record in records:
            key = str(record["source_key"])
            if key in conflicting_keys:
                continue
            previous = records_by_key.get(key)
            if previous is not None:
                if tuple(
                    previous[field] for field in ("brand", "rate", "channel", "identity")
                ) != tuple(record[field] for field in ("brand", "rate", "channel", "identity")):
                    problems.append(
                        _problem(
                            "conflicting_duplicate",
                            (
                                "The same Cactus stable identity has conflicting visible "
                                "fields across pages."
                            ),
                            source_key=key,
                            first=previous,
                            duplicate=record,
                        )
                    )
                    records_by_key.pop(key, None)
                    conflicting_keys.add(key)
                else:
                    duplicate_source_ids += 1
                continue
            records_by_key[key] = record
        for link in links:
            if link not in seen and link not in pending:
                pending.append(link)
        if len(seen) > DEFAULT_MAX_PAGES:
            raise OfficialPartnerSourceError("Cactus: pagination exceeded the safety limit")
    # A supplied mapping may include discovered pages that are not linked from
    # the first page; process them too, but never silently ignore a missing link.
    unvisited_supplied = sorted(set(normalized_pages) - seen)
    if unvisited_supplied:
        pending.extend(unvisited_supplied)
        while pending:
            page_url = pending.pop(0)
            if page_url in seen:
                continue
            seen.add(page_url)
            records, page_problems, links = _parse_cactus_page(normalized_pages[page_url], page_url)
            problems.extend(page_problems)
            for record in records:
                key = str(record["source_key"])
                if key in conflicting_keys:
                    continue
                previous = records_by_key.get(key)
                if previous is None:
                    records_by_key[key] = record
                elif tuple(
                    previous[field] for field in ("brand", "rate", "channel", "identity")
                ) == tuple(record[field] for field in ("brand", "rate", "channel", "identity")):
                    duplicate_source_ids += 1
                else:
                    records_by_key.pop(key, None)
                    conflicting_keys.add(key)
                    problems.append(
                        _problem(
                            "conflicting_duplicate",
                            (
                                "The same Cactus stable identity has conflicting visible "
                                "fields across pages."
                            ),
                            source_key=key,
                        )
                    )
            for link in links:
                if link not in seen and link in normalized_pages and link not in pending:
                    pending.append(link)
    name_duplicates, name_conflicts = _cactus_dedupe_name_identities(records_by_key, problems)
    duplicate_source_ids += name_duplicates
    offers = [
        _offer(
            source_key=str(record["source_key"]),
            brand_key=str(record["brand_key"]),
            brand=str(record["brand"]),
            card_id=CACTUS_CARD_ID,
            channel=str(record["channel"]),
            mode="total",
            reward_kind="points",
            value=str(record["rate"]),
            conditions=str(record["conditions"]),
            source_url=str(record["source_url"]),
        )
        for record in records_by_key.values()
    ]
    source = {
        "id": "mtbank_cactus_partners",
        "url": source_url,
        "pages": sorted(seen),
        "page_sha256": {
            page_url: _source_sha(normalized_pages[page_url]) for page_url in sorted(seen)
        },
        "card_id": CACTUS_CARD_ID,
    }
    counts = {
        "source_pages": len(seen),
        "source_items": len(records_by_key)
        + duplicate_source_ids
        + name_conflicts
        + sum(
            1
            for item in problems
            if str(item.get("kind"))
            in {"missing_reward", "ambiguous_reward", "missing_partner_url"}
        ),
        "unique_source_ids": len(records_by_key),
        "offers_emitted": len(offers),
        "held_items": len(problems),
        "duplicate_source_ids": duplicate_source_ids,
        "pagination_links": len(pagination_links),
    }
    return _build_collection(
        source=source,
        offers=offers,
        problems=problems,
        counts=counts,
        validation={
            "ok": True,
            "card_id": CACTUS_CARD_ID,
            "deduplication": (
                "stable partner URL, official image, or exact name identity; "
                "same normalized name/rate/channel prefers URL > image > name"
            ),
            "reward_kind": "points",
        },
    )


def collect_cactus(
    source_html: str, *, source_url: str = DEFAULT_CACTUS_URL
) -> PartnerSourceCollection:
    """Normalize one Cactus response; use :func:`fetch_cactus` for pagination."""

    return collect_cactus_pages({source_url: source_html}, source_url=source_url)


def fetch_cactus(
    url: str = DEFAULT_CACTUS_URL,
    *,
    timeout: float = 30.0,
    max_bytes: int = DEFAULT_MAX_HTML_BYTES,
    max_pages: int = DEFAULT_MAX_PAGES,
    fetcher: Callable[[str], str] | None = None,
) -> PartnerSourceCollection:
    """Fetch and normalize every linked Cactus pagination page."""

    source_url = _canonical_cactus_page_url(url)
    if not source_url:
        raise OfficialPartnerSourceError("Cactus: source URL is invalid")
    fetch = fetcher or (lambda page_url: _http_text(page_url, timeout=timeout, max_bytes=max_bytes))
    pages: dict[str, str] = {}
    pending = [source_url]
    seen: set[str] = set()
    while pending:
        page_url = pending.pop(0)
        if page_url in seen:
            continue
        seen.add(page_url)
        if len(seen) > max_pages:
            raise OfficialPartnerSourceError("Cactus: pagination exceeded the safety limit")
        body = fetch(page_url)
        pages[page_url] = body
        _records, _problems, links = _parse_cactus_page(body, page_url)
        for link in links:
            if link not in seen and link not in pending:
                pending.append(link)
    return collect_cactus_pages(pages, source_url=source_url)


# ---------------------------------------------------------------------------
# BNB 1-2-3


def _bnb_partner_id(value: str) -> str | None:
    match = re.search(r"#popupPartner-(\d+)", value)
    return match.group(1) if match else None


def _bnb_popup(root: _Node, partner_id: str) -> _Node:
    matches = [
        node for node in _descendants(root) if node.attr("id") == f"popupPartner-{partner_id}"
    ]
    if not matches:
        raise OfficialPartnerSourceError(
            f"BNB: expected at least one popupPartner-{partner_id}, found 0"
        )
    # The live page currently renders the same popup catalogue twice (desktop
    # and the secondary partner list).  Treat byte/layout differences that
    # normalize to the same visible text as one source record, but never pick a
    # winner if duplicate definitions disagree.
    signatures = {_text(match) for match in matches}
    if len(signatures) != 1:
        raise OfficialPartnerSourceError(
            f"BNB: duplicate popupPartner-{partner_id} definitions disagree"
        )
    return matches[0]


def _bnb_rate_lines(description: _Node) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    list_items = list(_descendants(description, tag="li"))
    # Most popups use a list for reward rows.  A few official popups (including
    # ELEMA/ETELIER) use separate paragraphs instead; only treat a paragraph as
    # a reward row when it carries an explicit channel/reward marker so a
    # marketing paragraph mentioning a percentage cannot become an offer.
    candidates: Iterable[_Node]
    if list_items:
        candidates = list_items
    else:
        candidates = (
            node
            for node in _descendants(description, tag="p")
            if _channel_from_text(_text(node)) is not None or "манибэк" in _text(node).casefold()
        )
    for item in candidates:
        values = _rates(_text(item))
        if len(values) == 1:
            lines.append((values[0], _text(item)))
        elif len(values) > 1:
            lines.append(("", _text(item)))
    return lines


def _bnb_semantics(root: _Node) -> bool:
    text = _text(root).casefold()
    return (
        "партнеры добавляют свой процент" in text
        and "дополнительный манибэк" in text
        and "указали общий" in text
    )


def _parse_bnb_partner(
    root: _Node, partner_id: str, source_url: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    popup = _bnb_popup(root, partner_id)
    title = _first_class(popup, "popup-modal_title", tag="h2")
    descriptions = _find_class(popup, "partner-info__descr")
    if title is None or not descriptions:
        raise OfficialPartnerSourceError(
            f"BNB: popup {partner_id} lost title or description selector"
        )
    name = _text(title)
    description = descriptions[-1]
    detail_text = _text(description)
    rows = _bnb_rate_lines(description)
    if not rows:
        values = _rates(detail_text)
        if len(values) == 1:
            rows = [(values[0], detail_text)]
        else:
            return [], [
                _problem(
                    "ambiguous_reward" if values else "missing_reward",
                    "BNB popup must contain one unambiguous partner reward line per channel.",
                    source_key=f"bnb:{partner_id}",
                    brand=name,
                    observed_rates=list(values),
                )
            ]
    parsed: list[tuple[str, str, str]] = []
    for rate, line in rows:
        if not rate:
            return [], [
                _problem(
                    "ambiguous_reward",
                    (
                        "A BNB reward line contains multiple percentages and cannot be "
                        "represented safely."
                    ),
                    source_key=f"bnb:{partner_id}",
                    brand=name,
                    line=line,
                )
            ]
        channel = _channel_from_text(line)
        if channel is None:
            channel = _channel_from_text(detail_text)
        if channel is None:
            channel = "any"
        parsed.append((rate, channel, line))
    # Collapse equal online/offline lines into one any-channel offer.  Distinct
    # values with no explicit channel are unsafe and remain held.
    by_channel: dict[str, set[str]] = {}
    for rate, channel, _line in parsed:
        by_channel.setdefault(channel, set()).add(rate)
    if "any" in by_channel and len(by_channel) > 1:
        return [], [
            _problem(
                "ambiguous_channel",
                "BNB popup combines an unscoped rate with channel-specific rates.",
                source_key=f"bnb:{partner_id}",
                brand=name,
            )
        ]
    if any(len(values) > 1 for values in by_channel.values()):
        return [], [
            _problem(
                "ambiguous_reward",
                (
                    "BNB popup has multiple rates for one channel; the v1 shape cannot "
                    "encode the category split."
                ),
                source_key=f"bnb:{partner_id}",
                brand=name,
                observed_rates=sorted({rate for rate, _channel, _line in parsed}),
            )
        ]
    if set(by_channel) == {"online", "offline"} and by_channel["online"] == by_channel["offline"]:
        by_channel = {"any": by_channel["online"]}
    offers = [
        _offer(
            source_key=f"bnb:{partner_id}:{channel}",
            brand_key=f"brand:bnb:{partner_id}",
            brand=name,
            card_id=BNB_CARD_ID,
            channel=channel,
            mode="total",
            reward_kind="cash",
            value=next(iter(values)),
            conditions=(
                "Источник указывает общий максимальный процент манибэка; ставку не следует "
                "повторно складывать с базовым манибэком БНБ-Банка; "
                f"условия источника: {detail_text}."
            ),
            source_url=source_url,
        )
        for channel, values in sorted(by_channel.items())
    ]
    return offers, []


def collect_bnb(source_html: str, *, source_url: str = DEFAULT_BNB_URL) -> PartnerSourceCollection:
    """Normalize the deduplicated BNB 1-2-3 popup catalogue."""

    source_url = _normalize_url(source_url)
    if not source_url:
        raise OfficialPartnerSourceError("BNB: source URL is invalid")
    root = _parse_html(source_html, "BNB")
    anchors = [
        anchor
        for anchor in _descendants(root, tag="a")
        if anchor.has_class("partner") and _bnb_partner_id(anchor.attr("href") or "")
    ]
    if not anchors:
        raise OfficialPartnerSourceError("BNB: partner anchor selector disappeared")
    if not _bnb_semantics(root):
        raise OfficialPartnerSourceError(
            "BNB: page-level total/additional semantics are missing or changed"
        )
    labels: dict[str, list[tuple[str, str]]] = {}
    for anchor in anchors:
        partner_id = _bnb_partner_id(anchor.attr("href") or "")
        assert partner_id is not None
        label = _first_class(anchor, "label_manyback")
        title = _first_class(anchor, "partner__title")
        if label is None or title is None:
            raise OfficialPartnerSourceError(
                f"BNB: partner anchor {partner_id} lost label or title"
            )
        labels.setdefault(partner_id, []).append((_text(label), _text(title)))
    offers: list[dict[str, object]] = []
    problems: list[dict[str, object]] = []
    duplicate_source_ids = 0
    for partner_id in sorted(labels, key=lambda value: int(value)):
        observed_labels = labels[partner_id]
        if (
            len({label for label, _title in observed_labels}) != 1
            or len({title for _label, title in observed_labels}) != 1
        ):
            problems.append(
                _problem(
                    "conflicting_duplicate",
                    "Duplicated BNB partner tiles disagree in visible label or name.",
                    source_key=f"bnb:{partner_id}",
                    observed=observed_labels,
                )
            )
            continue
        if len(observed_labels) > 1:
            duplicate_source_ids += len(observed_labels) - 1
        partner_offers, partner_problems = _parse_bnb_partner(root, partner_id, source_url)
        offers.extend(partner_offers)
        problems.extend(partner_problems)
    source = {
        "id": "bnb_1_2_3_partners",
        "url": source_url,
        "scope": "popupPartner-*",
        "card_id": BNB_CARD_ID,
        "html_sha256": _source_sha(source_html),
    }
    return _build_collection(
        source=source,
        offers=offers,
        problems=problems,
        counts={
            "source_items": len(labels),
            "unique_source_ids": len(labels),
            "offers_emitted": len(offers),
            "held_items": len(problems),
            "duplicate_source_ids": duplicate_source_ids,
            "popup_definitions": len(
                [
                    node
                    for node in _descendants(root)
                    if (node.attr("id") or "").startswith("popupPartner-")
                ]
            ),
        },
        validation={
            "ok": True,
            "card_id": BNB_CARD_ID,
            "mode": "total",
            "reward_kind": "cash",
            "deduplication": "numeric popup partner ID",
        },
    )


def fetch_bnb(
    url: str = DEFAULT_BNB_URL,
    *,
    timeout: float = 30.0,
    max_bytes: int = DEFAULT_MAX_HTML_BYTES,
) -> PartnerSourceCollection:
    """Fetch the public BNB catalogue and normalize it."""

    source_url = _normalize_url(url)
    return collect_bnb(
        _http_text(source_url, timeout=timeout, max_bytes=max_bytes),
        source_url=source_url,
    )


# ---------------------------------------------------------------------------
# Belarusbank Izi


def _extract_balanced(source: str, start: int, opening: str, closing: str) -> str:
    if start < 0 or start >= len(source) or source[start] != opening:
        raise OfficialPartnerSourceError("Izi: JavaScript array start is missing")
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise OfficialPartnerSourceError("Izi: unterminated JavaScript array")


def _js_unescape(value: str) -> str:
    replacements = {
        "\\n": "\n",
        "\\r": "\r",
        "\\t": "\t",
        "\\/": "/",
        "\\'": "'",
        '\\"': '"',
        "\\\\": "\\",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return value


def _js_string(array_text: str, field: str) -> str:
    marker = re.search(rf"'{re.escape(field)}'\s*:\s*'", array_text)
    if marker is None:
        return ""
    start = marker.end()
    chars: list[str] = []
    escaped = False
    for index in range(start, len(array_text)):
        char = array_text[index]
        if escaped:
            chars.append("\\" + char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "'":
            return _js_unescape("".join(chars))
        else:
            chars.append(char)
    return ""


def _izi_location_objects(source_html: str) -> list[dict[str, str]]:
    marker = re.search(r"\bvar\s+locations\s*=\s*", source_html)
    if marker is None:
        raise OfficialPartnerSourceError("Izi: locations JavaScript variable is missing")
    array_start = source_html.find("[", marker.end())
    array_text = _extract_balanced(source_html, array_start, "[", "]")
    objects: list[dict[str, str]] = []
    for match in re.finditer(r"\{", array_text):
        try:
            object_text = _extract_balanced(array_text, match.start(), "{", "}")
        except OfficialPartnerSourceError:
            continue
        item_id = _js_string(object_text, "id")
        coords_text = _js_string(object_text, "coords")
        name_html = _js_string(object_text, "name")
        if not item_id or not name_html:
            continue
        coords_match = re.search(r"'coords'\s*:\s*\[\s*'([^']*)'\s*,\s*'([^']*)'", object_text)
        objects.append(
            {
                "id": item_id,
                "coords": ",".join(coords_match.groups()) if coords_match else coords_text,
                "name_html": name_html,
            }
        )
    if not objects:
        raise OfficialPartnerSourceError("Izi: locations array contains no valid objects")
    return objects


def _izi_location_metadata(item: Mapping[str, str], source_url: str) -> dict[str, str]:
    root = _parse_html(item["name_html"], "Izi location")
    paragraphs = list(_descendants(root, tag="p"))
    if len(paragraphs) < 3:
        raise OfficialPartnerSourceError(
            "Izi: location object lost entity/store/address paragraphs"
        )
    entity = _text(paragraphs[0])
    store = _text(paragraphs[1])
    address = _text(paragraphs[2])
    detail_anchor = next(iter(_descendants(root, tag="a")), None)
    detail_url = _normalize_url(
        detail_anchor.attr("href") if detail_anchor else "", base_url=source_url
    )
    if not entity or not store or not address or not detail_url:
        raise OfficialPartnerSourceError(
            "Izi: location object contains incomplete exact outlet evidence"
        )
    return {
        "id": item["id"],
        "entity": entity,
        "store": store,
        "address": address,
        "detail_url": detail_url,
    }


def _alphanumeric_label_key(value: str, *, replace_yo: bool = False) -> str:
    """Return a conservative Unicode/casefolded alphanumeric label key."""

    normalized = unicodedata.normalize("NFC", _normalize_space(value)).casefold()
    if replace_yo:
        normalized = normalized.replace("ё", "е")
    alphanumeric = "".join(character for character in normalized if character.isalnum())
    return alphanumeric or f"__empty__:{normalized}"


def _izi_store_label_key(value: str) -> str:
    """Return a conservative same-entity store-label identity.

    Only the alphanumeric content participates in the identity.  Unicode NFC
    keeps letters intact, casefold makes case variants equal, and Belarusian
    ``ё`` is aligned with the spelling used by the source's other rows.  A
    punctuation-only label is kept distinct rather than collapsing every such
    source row into one empty key.
    """

    return _alphanumeric_label_key(value, replace_yo=True)


def _izi_store_label_display_key(value: str) -> tuple[int, int, str, str]:
    """Order equivalent source labels, preferring unquoted short text."""

    normalized = _normalize_space(value)
    punctuation = sum(
        1 for character in normalized if unicodedata.category(character).startswith("P")
    )
    return (punctuation, len(normalized), normalized.casefold(), normalized)


def _izi_row_channel(address: str, store: str) -> str:
    """Classify one Izi outlet from the source's exact visible fields.

    Belarusbank uses ``Интернет-магазин`` as the explicit online marker in
    the store-name column.  Any other complete address/store pair is a
    physical outlet.  A placeholder or incomplete pair is deliberately
    unknown: it must not be widened to ``any`` without source evidence.
    """

    if _IZI_ONLINE_STORE_RE.search(store):
        return "online"
    known_address = bool(address) and not _MISSING_LOCATION_RE.fullmatch(address)
    known_store = bool(store) and not _MISSING_LOCATION_RE.fullmatch(store)
    if known_address and known_store:
        return "offline"
    return "unknown"


def _izi_detail_rows(source_html: str, detail_url: str) -> tuple[str, list[dict[str, str]]]:
    root = _parse_html(source_html, "Izi detail")
    page_text = _text(root)
    if "изи-карта" not in page_text.casefold() and "izi" not in detail_url.casefold():
        raise OfficialPartnerSourceError(f"Izi: detail page {detail_url} is not card-specific")
    tables = list(_descendants(root, tag="table"))
    expected_headers = {
        (
            "адрес",
            "наименование организации торговли (сервиса)",
            "mcc",
            "кэш бэк",
        ),
        # Keep the fixture/older wording explicit; do not fuzzy-match a
        # partially changed table because column meaning is safety-critical.
        (
            "адрес",
            "наименование организации (сервиса)",
            "mcc",
            "кэш бэк",
        ),
    }
    table_headers = {
        id(table): tuple(_text(th).casefold() for th in _descendants(table, tag="th"))
        for table in tables
    }
    matching = [table for table in tables if table_headers[id(table)][:4] in expected_headers]

    def table_pairs(table: _Node, expected: tuple[str, str]) -> list[tuple[str, str]]:
        if table_headers[id(table)] != expected:
            raise OfficialPartnerSourceError(f"Izi: unexpected split-table headers on {detail_url}")
        pairs: list[tuple[str, str]] = []
        for row_index, row in enumerate(_descendants(table, tag="tr")):
            cells = list(_descendants(row, tag="td"))
            if not cells:
                continue
            if len(cells) != 2:
                raise OfficialPartnerSourceError(
                    f"Izi: split cashback table row {row_index} has unexpected cells"
                )
            address, value = (_text(cell) for cell in cells)
            if not address or not value:
                raise OfficialPartnerSourceError(
                    f"Izi: split cashback table row {row_index} lost address or value"
                )
            pairs.append((address, value))
        if not pairs:
            raise OfficialPartnerSourceError(
                f"Izi: split cashback table {detail_url} contains no rows"
            )
        return pairs

    combined_table = [table for table in matching if len(table_headers[id(table)]) >= 4]
    split_by_kind = {
        "stores": {
            ("адрес", "наименование организации торговли (сервиса)"),
            ("адрес", "наименование организации (сервиса)"),
        },
        "mcc": {("адрес", "mcc")},
        "rates": {("адрес", "кэш бэк")},
    }
    if len(combined_table) == 1 and len(matching) == 1:
        combined = combined_table[0]
        row_sets: list[tuple[str, str, str, str]] = []
        for row_index, row in enumerate(_descendants(combined, tag="tr")):
            cells = list(_descendants(row, tag="td"))
            if not cells:
                continue
            if len(cells) != 4:
                raise OfficialPartnerSourceError(
                    f"Izi: cashback table row {row_index} has unexpected cells"
                )
            row_sets.append(tuple(_text(cell) for cell in cells))  # type: ignore[arg-type]
    else:
        # Some current detail pages split the same logical rows into three
        # adjacent two-column tables.  Merge only when all three exact table
        # schemas occur once and their address sequences match positionally.
        candidates: dict[str, list[_Node]] = {}
        for kind, header_options in split_by_kind.items():
            candidates[kind] = [
                table for table in tables if table_headers[id(table)] in header_options
            ]
        if any(len(items) != 1 for items in candidates.values()):
            raise OfficialPartnerSourceError(
                f"Izi: expected one exact cashback table on {detail_url}"
            )
        store_pairs = table_pairs(
            candidates["stores"][0], table_headers[id(candidates["stores"][0])]
        )
        mcc_pairs = table_pairs(candidates["mcc"][0], ("адрес", "mcc"))
        rate_pairs = table_pairs(candidates["rates"][0], ("адрес", "кэш бэк"))
        if not (len(store_pairs) == len(mcc_pairs) == len(rate_pairs)):
            raise OfficialPartnerSourceError(
                f"Izi: split cashback tables have different row counts on {detail_url}"
            )
        if any(
            store_pairs[index][0] != mcc_pairs[index][0]
            or store_pairs[index][0] != rate_pairs[index][0]
            for index in range(len(store_pairs))
        ):
            raise OfficialPartnerSourceError(
                f"Izi: split cashback tables have different address order on {detail_url}"
            )
        row_sets = [
            (
                store_pairs[index][0],
                store_pairs[index][1],
                mcc_pairs[index][1],
                rate_pairs[index][1],
            )
            for index in range(len(store_pairs))
        ]
    breadcrumbs = [
        _text(node)
        for node in _descendants(root)
        if node.has_class("breadcrumbs__link") and _text(node)
    ]
    entity = breadcrumbs[-1] if breadcrumbs else ""
    rows: list[dict[str, str]] = []
    for row_index, (address, store, mcc, rate_text) in enumerate(row_sets):
        if not address or not store or not _MCC_RE.fullmatch(mcc):
            raise OfficialPartnerSourceError(
                f"Izi: cashback row {row_index} lost address, store, or MCC"
            )
        # Belarusbank renders this column as a bare decimal (for example
        # ``2,0``), unlike the other catalogues which include a percent sign.
        # Accept an optional sign but require the whole cell to be one finite
        # percentage; a free-form string remains unsafe and is held.
        rate_match = re.fullmatch(r"(?P<value>\d+(?:[.,]\d+)?)\s*%?", rate_text)
        rates = ()
        if rate_match:
            try:
                parsed = Decimal(rate_match.group("value").replace(",", "."))
            except InvalidOperation:
                parsed = Decimal("-1")
            if parsed.is_finite() and 0 <= parsed <= 100:
                rates = (format(parsed.normalize(), "f"),)
        if len(rates) != 1:
            raise OfficialPartnerSourceError(f"Izi: cashback row {row_index} has ambiguous reward")
        rows.append(
            {
                "address": address,
                "store": store,
                "mcc": mcc,
                "rate": rates[0],
                "entity": entity,
                "channel": _izi_row_channel(address, store),
            }
        )
    if not rows:
        raise OfficialPartnerSourceError(f"Izi: cashback table {detail_url} contains no rows")
    return entity, rows


def _izi_public_preview(*, brand: str, value: str, conditions: str) -> str:
    """Mirror the public no-MCC store card for the Izi offer budget check.

    The source URL is intentionally not part of this preview: the public store
    card currently renders the partner name, card reward, conditions, and the
    standard MCC warnings.  HTML escaping is included because that is what the
    Telegram renderer applies to each user-visible value.
    """

    return (
        f"🏪 <b>{html_lib.escape(brand)}</b>\n\n"
        "<b>🎁 Партнёрская выгода</b>\n"
        f"• {html_lib.escape('Изи-карта')} — {value}% деньгами · итоговая выгода\n"
        f"  {html_lib.escape(conditions)}\n\n"
        "⚠️ MCC магазина пока не указан; перед оплатой проверьте его в банке.\n\n"
        "<i>MCC может отличаться у разных касс и способов оплаты.</i>"
    )


def _izi_condition_with_budget(
    *,
    entity: str,
    store: str,
    addresses: Sequence[str],
    mccs: Sequence[str],
    value: str,
) -> str:
    """Keep complete Izi addresses unless the rendered public card is too long.

    Legal entity and store names are retained in both forms.  When the full
    address list cannot fit, the fallback keeps the authoritative outlet count
    and MCCs and explicitly says that the address list was shortened.  The
    final character-level fallback is only for pathological source rows with
    unusually long names or MCC lists.
    """

    address_list = "; ".join(addresses)
    full = (
        f"Точная точка Изи-карты: организация «{entity}»; торговая точка "
        f"«{store}»; официальных точек: {len(addresses)}; адреса: {address_list}; "
        f"MCC: {', '.join(mccs)}."
    )
    if (
        _telegram_length(_izi_public_preview(brand=store, value=value, conditions=full))
        <= TELEGRAM_SAFE_MESSAGE_LENGTH
    ):
        return full

    summarized = (
        f"Точная точка Изи-карты: организация «{entity}»; торговая точка "
        f"«{store}»; официальных точек: {len(addresses)}; адреса: список "
        f"сокращён для сообщения; MCC: {', '.join(mccs)}."
    )
    if (
        _telegram_length(_izi_public_preview(brand=store, value=value, conditions=summarized))
        <= TELEGRAM_SAFE_MESSAGE_LENGTH
    ):
        return summarized

    # Preserve the stable prefix and the explicit fact that addresses were
    # shortened while fitting the remainder to the same final rendered-card
    # budget.  This branch is not reached by the current source response but
    # prevents a future unusually long legal/entity name from breaking sends.
    marker = "…"
    prefix = (
        f"Точная точка Изи-карты: организация «{entity}»; торговая точка "
        f"«{store}»; официальных точек: {len(addresses)}; адреса: список "
        f"сокращён для сообщения; MCC: {', '.join(mccs)}."
    )
    if (
        _telegram_length(_izi_public_preview(brand=store, value=value, conditions=marker))
        > TELEGRAM_SAFE_MESSAGE_LENGTH
    ):
        # The names themselves can be externally supplied and are not bounded
        # by the source.  A concise safe sentence remains useful to the public
        # renderer in this pathological case.
        minimal = (
            f"Изи-карта: адреса сокращены; официальных точек: {len(addresses)}; "
            f"MCC: {', '.join(mccs)}."
        )
        if (
            _telegram_length(_izi_public_preview(brand=store, value=value, conditions=minimal))
            <= TELEGRAM_SAFE_MESSAGE_LENGTH
        ):
            return minimal
        prefix = "Изи-карта: адреса сокращены."

    low, high = 0, len(prefix)
    best = marker
    while low <= high:
        middle = (low + high) // 2
        candidate = prefix[:middle].rstrip() + marker
        if (
            _telegram_length(_izi_public_preview(brand=store, value=value, conditions=candidate))
            <= TELEGRAM_SAFE_MESSAGE_LENGTH
        ):
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def collect_izi(
    source_html: str,
    *,
    detail_pages: Mapping[str, str] | None = None,
    source_url: str = DEFAULT_IZI_URL,
) -> PartnerSourceCollection:
    """Normalize Izi location metadata and supplied detail-table responses."""

    source_url = _normalize_url(source_url)
    if not source_url:
        raise OfficialPartnerSourceError("Izi: source URL is invalid")
    locations = [
        _izi_location_metadata(item, source_url) for item in _izi_location_objects(source_html)
    ]
    by_id: dict[str, list[dict[str, str]]] = {}
    for location in locations:
        by_id.setdefault(location["id"], []).append(location)
    problems: list[dict[str, object]] = []
    offer_candidates: list[tuple[dict[str, object], str, str, str]] = []
    detail_pages = detail_pages or {}
    for item_id in sorted(by_id, key=lambda value: int(value) if value.isdigit() else value):
        metadata = by_id[item_id]
        detail_url = metadata[0]["detail_url"]
        if any(item["detail_url"] != detail_url for item in metadata):
            problems.append(
                _problem(
                    "conflicting_detail_url",
                    "One Izi entity ID points to different detail URLs.",
                    source_key=f"izi:{item_id}",
                )
            )
            continue
        entity_names = {item["entity"] for item in metadata}
        if len(entity_names) != 1:
            problems.append(
                _problem(
                    "conflicting_entity",
                    "One Izi entity ID has conflicting legal-entity names.",
                    source_key=f"izi:{item_id}",
                    observed_entities=sorted(entity_names),
                )
            )
            continue
        detail = detail_pages.get(detail_url)
        if detail is None:
            problems.append(
                _problem(
                    "missing_detail",
                    "Izi exact outlet rows require the linked official detail page.",
                    source_key=f"izi:{item_id}",
                    source_url=detail_url,
                )
            )
            continue
        try:
            detail_entity, rows = _izi_detail_rows(detail, detail_url)
        except OfficialPartnerSourceError as exc:
            problems.append(
                _problem(
                    "detail_structure", str(exc), source_key=f"izi:{item_id}", source_url=detail_url
                )
            )
            continue
        expected_entity = next(iter(entity_names))
        if detail_entity and detail_entity != expected_entity:
            problems.append(
                _problem(
                    "conflicting_entity",
                    "Izi detail breadcrumb disagrees with the exact entity in the map source.",
                    source_key=f"izi:{item_id}",
                    map_entity=expected_entity,
                    detail_entity=detail_entity,
                )
            )
            continue
        metadata_keys = {
            (item["address"], _izi_store_label_key(item["store"])): item["store"]
            for item in metadata
        }
        groups: dict[str, dict[str, object]] = {}
        for row in rows:
            row_entity = row["entity"] or expected_entity
            if row_entity and row_entity != expected_entity:
                problems.append(
                    _problem(
                        "conflicting_entity",
                        "Izi detail row entity disagrees with the exact entity in the map source.",
                        source_key=f"izi:{item_id}",
                        detail_entity=row_entity,
                        map_entity=expected_entity,
                    )
                )
                continue
            store_key = _izi_store_label_key(row["store"])
            metadata_keys.pop((row["address"], store_key), None)
            brand_key = f"brand:izi:{item_id}:{_stable_hash(store_key)}"
            group = groups.setdefault(
                brand_key,
                {
                    "stores": set(),
                    "rates": set(),
                    "channels": set(),
                    "outlets": set(),
                },
            )
            group["stores"].add(row["store"])  # type: ignore[union-attr]
            group["rates"].add(row["rate"])  # type: ignore[union-attr]
            group["channels"].add(row.get("channel", "unknown"))  # type: ignore[union-attr]
            group["outlets"].add(  # type: ignore[union-attr]
                (row["address"], row["store"], row["mcc"])
            )
        for brand_key in sorted(groups):
            group = groups[brand_key]
            display_store = min(
                group["stores"],
                key=_izi_store_label_display_key,  # type: ignore[arg-type]
            )
            rates = sorted(group["rates"])  # type: ignore[arg-type]
            channels = sorted(group["channels"])  # type: ignore[arg-type]
            outlets = sorted(group["outlets"])  # type: ignore[arg-type]
            if any(channel not in {"online", "offline"} for channel in channels):
                problems.append(
                    _problem(
                        "unknown_channel",
                        (
                            "Izi detail rows do not contain an explicit online marker or a "
                            "complete physical address/store pair; the group is held."
                        ),
                        source_key=brand_key,
                        brand=display_store,
                        observed_rates=rates,
                        observed_channels=channels,
                        outlets=[
                            {"address": address, "store": store, "mcc": mcc}
                            for address, store, mcc in outlets
                        ],
                    )
                )
                continue
            if len(rates) != 1 or len(channels) != 1:
                problems.append(
                    _problem(
                        "conflicting_brand_offer",
                        (
                            "Izi detail rows sharing one brand_key have conflicting "
                            "rates or payment channels; the group is held."
                        ),
                        source_key=brand_key,
                        brand=display_store,
                        observed_rates=rates,
                        observed_channels=channels,
                        outlets=[
                            {"address": address, "store": store, "mcc": mcc}
                            for address, store, mcc in outlets
                        ],
                    )
                )
                continue
            addresses = sorted({address for address, _store, _mcc in outlets})
            mccs = sorted({mcc for _address, _store, mcc in outlets})
            source_key = f"izi:{item_id}:brand:{_stable_hash(brand_key)}"
            condition = _izi_condition_with_budget(
                entity=expected_entity,
                store=str(display_store),
                addresses=addresses,
                mccs=mccs,
                value=rates[0],
            )
            offer_candidates.append(
                (
                    _offer(
                        source_key=source_key,
                        brand_key=brand_key,
                        brand=str(display_store),
                        card_id=IZI_CARD_ID,
                        channel=channels[0],
                        mode="total",
                        reward_kind="cash",
                        value=rates[0],
                        conditions=condition,
                        source_url=detail_url,
                    ),
                    _izi_store_label_key(str(display_store)),
                    item_id,
                    expected_entity,
                )
            )
        # Detail pages can legitimately expose all outlets for an entity, so a
        # map coordinate is not required for every detail row.  The reverse
        # mismatch is still useful evidence and is held, not guessed.
        if metadata_keys:
            problems.append(
                _problem(
                    "location_not_in_detail",
                    "An exact Izi map outlet was not found in its detail table.",
                    source_key=f"izi:{item_id}",
                    outlets=sorted(
                        {f"{address} / {store}" for (address, _key), store in metadata_keys.items()}
                    ),
                )
            )

    # A store label is not globally unique: separate Izi entity IDs can use
    # the same visible name.  Keep each public label source-distinct while
    # retaining the exact legal entity supplied by the detail page.
    label_entities: dict[str, set[str]] = {}
    for _candidate, label_key, item_id, _entity in offer_candidates:
        label_entities.setdefault(label_key, set()).add(item_id)
    colliding_labels = {label for label, item_ids in label_entities.items() if len(item_ids) > 1}
    offers: list[dict[str, object]] = []
    for candidate, label_key, _item_id, entity in offer_candidates:
        if label_key in colliding_labels:
            candidate["brand"] = _normalize_space(f"{candidate['brand']} — {entity}")
        offers.append(candidate)

    source = {
        "id": "belarusbank_izi_partners",
        "url": source_url,
        "scope": "var locations + linked detail tables",
        "card_id": IZI_CARD_ID,
        "html_sha256": _source_sha(source_html),
        "detail_sha256": {
            detail_url: _source_sha(detail_pages[detail_url]) for detail_url in sorted(detail_pages)
        },
    }
    return _build_collection(
        source=source,
        offers=offers,
        problems=problems,
        counts={
            "source_items": len(locations),
            "unique_source_ids": len(by_id),
            "detail_pages": len(detail_pages),
            "offers_emitted": len(offers),
            "held_items": len(problems),
            "duplicate_source_ids": len(locations)
            - len({(item["id"], item["address"], item["store"]) for item in locations}),
        },
        validation={
            "ok": True,
            "card_id": IZI_CARD_ID,
            "reward_kind": "cash",
            "deduplication": "entity ID + brand_key; exact outlet/address/MCC summary",
            "fuzzy_mapping": False,
        },
    )


def fetch_izi(
    url: str = DEFAULT_IZI_URL,
    *,
    timeout: float = 30.0,
    max_bytes: int = DEFAULT_MAX_HTML_BYTES,
    max_details: int = DEFAULT_MAX_DETAILS,
    fetcher: Callable[[str], str] | None = None,
) -> PartnerSourceCollection:
    """Fetch the Izi map and every unique linked detail page."""

    source_url = _normalize_url(url)
    fetch = fetcher or (lambda page_url: _http_text(page_url, timeout=timeout, max_bytes=max_bytes))
    listing = fetch(source_url)
    locations = [
        _izi_location_metadata(item, source_url) for item in _izi_location_objects(listing)
    ]
    detail_urls = sorted({item["detail_url"] for item in locations})
    if len(detail_urls) > max_details:
        raise OfficialPartnerSourceError("Izi: detail-page count exceeds the safety limit")
    details = {detail_url: fetch(detail_url) for detail_url in detail_urls}
    return collect_izi(listing, detail_pages=details, source_url=source_url)


# ---------------------------------------------------------------------------
# Statuscard


def _status_id(node: _Node) -> str | None:
    value = node.attr("id") or ""
    match = re.search(r"_(\d+)$", value)
    return match.group(1) if match else None


def _status_item_nodes(root: _Node) -> list[_Node]:
    return [node for node in _descendants(root, tag="div") if node.has_class("partner-list__item")]


def _status_list_records(
    source_html: str, source_url: str
) -> tuple[dict[str, dict[str, str]], list[dict[str, object]]]:
    root = _parse_html(source_html, "Statuscard AJAX")
    items = _status_item_nodes(root)
    if not items:
        raise OfficialPartnerSourceError("Statuscard: partner-list__item selector disappeared")
    records: dict[str, dict[str, str]] = {}
    problems: list[dict[str, object]] = []
    for item in items:
        item_id = _status_id(item)
        link = _first_class(item, "partner-list__item-link", tag="a")
        logo = _first_class(item, "partner-list__item-logo")
        promo = _first_class(item, "partner-list__item-promo")
        if item_id is None or link is None or logo is None or promo is None:
            raise OfficialPartnerSourceError(
                "Statuscard: partner tile lost ID, detail link, name, or promo selector"
            )
        name = _text(logo)
        promo_text = _text(promo)
        rates = _rates(promo_text)
        source_key = f"statuscard:{item_id}"
        if "манибэк" not in promo_text.casefold() or len(rates) != 1:
            problems.append(
                _problem(
                    "discount_not_cashback"
                    if "скид" in promo_text.casefold()
                    else "ambiguous_reward",
                    "Statuscard list item is not one unambiguous maniback percentage.",
                    source_key=source_key,
                    brand=name,
                    observed_promo=promo_text,
                )
            )
            continue
        detail_url = _normalize_url(link.attr("href") or "", base_url=source_url)
        if not detail_url:
            raise OfficialPartnerSourceError(f"Statuscard: detail URL for {item_id} is invalid")
        records[item_id] = {"id": item_id, "name": name, "rate": rates[0], "detail_url": detail_url}
    return records, problems


def _status_detail(record: Mapping[str, str], source_html: str) -> dict[str, str]:
    root = _parse_html(source_html, "Statuscard detail")
    heading = next(iter(_descendants(root, tag="h1")), None)
    rate_node = _first_class(root, "upper")
    content = _first_class(root, "partner-popup__content")
    if heading is None or content is None:
        raise OfficialPartnerSourceError("Statuscard: detail page lost heading or content selector")
    name = _text(heading)
    if name != record["name"]:
        raise OfficialPartnerSourceError("Statuscard: list and detail reward/name disagree")
    rate = record["rate"]
    if rate_node is not None:
        reward_text = _text(rate_node)
        rates = _rates(reward_text)
        if "манибэк" not in reward_text.casefold() or len(rates) != 1:
            raise OfficialPartnerSourceError(
                "Statuscard: detail reward is not one cashback percentage"
            )
        if rates[0] != record["rate"]:
            raise OfficialPartnerSourceError("Statuscard: list and detail reward/name disagree")
        rate = rates[0]
    body = _text(content)
    channel = _channel_from_text(body)
    if channel is None:
        # An explicit address with no online marker is an offline store offer.
        channel = (
            "offline"
            if _first_class(content, "partner-filter__item") is not None
            or "адрес" in body.casefold()
            else None
        )
    if channel is None:
        raise OfficialPartnerSourceError(
            "Statuscard: detail page does not state an applicable payment channel"
        )
    return {"name": name, "rate": rate, "channel": channel, "conditions": body}


def collect_statuscard(
    source_html: str,
    *,
    detail_pages: Mapping[str, str] | None = None,
    source_url: str = DEFAULT_STATUS_URL,
) -> PartnerSourceCollection:
    """Normalize StatusBank's maniback AJAX list and detail pages."""

    source_url = _normalize_url(source_url)
    if not source_url:
        raise OfficialPartnerSourceError("Statuscard: source URL is invalid")
    records, problems = _status_list_records(source_html, source_url)
    list_source_items = len(records) + len(problems)
    detail_pages = detail_pages or {}
    offers: list[dict[str, object]] = []
    for item_id in sorted(records, key=lambda value: int(value)):
        record = records[item_id]
        detail_url = record["detail_url"]
        detail = detail_pages.get(detail_url)
        if detail is None:
            problems.append(
                _problem(
                    "missing_detail",
                    "Statuscard maniback rows require the linked official detail page.",
                    source_key=f"statuscard:{item_id}",
                    source_url=detail_url,
                )
            )
            continue
        try:
            normalized = _status_detail(record, detail)
        except OfficialPartnerSourceError as exc:
            problems.append(
                _problem(
                    "detail_structure",
                    str(exc),
                    source_key=f"statuscard:{item_id}",
                    source_url=detail_url,
                )
            )
            continue
        offers.append(
            _offer(
                source_key=f"statuscard:{item_id}:{normalized['channel']}",
                brand_key=f"brand:statuscard:{item_id}",
                brand=normalized["name"],
                card_id=STATUS_CARD_ID,
                channel=normalized["channel"],
                mode="total",
                reward_kind="cash",
                value=normalized["rate"],
                conditions=f"Условия партнёра Статускарты: {normalized['conditions']}",
                source_url=detail_url,
            )
        )
    source = {
        "id": "statusbank_statuscard_partners",
        "url": source_url,
        "scope": "typepartner=Манибэк",
        "card_id": STATUS_CARD_ID,
        "html_sha256": _source_sha(source_html),
        "detail_sha256": {
            detail_url: _source_sha(detail_pages[detail_url]) for detail_url in sorted(detail_pages)
        },
    }
    return _build_collection(
        source=source,
        offers=offers,
        problems=problems,
        counts={
            "source_items": list_source_items,
            "unique_source_ids": len(records),
            "detail_pages": len(detail_pages),
            "offers_emitted": len(offers),
            "held_items": len(problems),
            "duplicate_source_ids": 0,
        },
        validation={
            "ok": True,
            "card_id": STATUS_CARD_ID,
            "reward_kind": "cash",
            "discounts_ignored": True,
            "deduplication": "numeric StatusBank element ID",
        },
    )


def fetch_statuscard(
    url: str = DEFAULT_STATUS_URL,
    *,
    timeout: float = 30.0,
    max_bytes: int = DEFAULT_MAX_HTML_BYTES,
    max_details: int = DEFAULT_MAX_DETAILS,
    fetcher: Callable[[str], str] | None = None,
) -> PartnerSourceCollection:
    """Fetch StatusBank's maniback list and linked detail pages."""

    source_url = _normalize_url(url)
    fetch = fetcher or (lambda page_url: _http_text(page_url, timeout=timeout, max_bytes=max_bytes))
    list_html = fetch(
        source_url
        + ("&" if "?" in source_url else "?")
        + "typepartner=%D0%9C%D0%B0%D0%BD%D0%B8%D0%B1%D1%8D%D0%BA"
    )
    records, _problems = _status_list_records(list_html, source_url)
    if len(records) > max_details:
        raise OfficialPartnerSourceError("Statuscard: detail-page count exceeds the safety limit")
    details = {record["detail_url"]: fetch(record["detail_url"]) for record in records.values()}
    return collect_statuscard(list_html, detail_pages=details, source_url=source_url)


# ---------------------------------------------------------------------------
# Aggregate and CLI helpers


def combine_collections(collections: Iterable[PartnerSourceCollection]) -> PartnerSourceCollection:
    """Combine independent source snapshots while retaining per-source counts."""

    all_offers: list[dict[str, object]] = []
    all_exclusions: list[dict[str, object]] = []
    all_problems: list[dict[str, object]] = []
    source_reports: list[dict[str, object]] = []
    for collection in collections:
        all_offers.extend(collection.snapshot["offers"])  # type: ignore[arg-type]
        all_exclusions.extend(collection.snapshot["exclusions"])  # type: ignore[arg-type]
        all_problems.extend(collection.snapshot["problems"])  # type: ignore[arg-type]
        source_reports.append(collection.report)
    return _build_collection(
        source={
            "id": "official_partner_sources",
            "sources": [report["source"] for report in source_reports],
        },
        offers=all_offers,
        exclusions=all_exclusions,
        problems=all_problems,
        counts={
            "sources": len(source_reports),
            "offers_emitted": len(all_offers),
            "exclusions_emitted": len(all_exclusions),
            "held_items": len(all_problems),
        },
        validation={
            "ok": all(not report.get("problems") for report in source_reports),
            "per_source": {
                str(report["source"]["id"]): report["counts"]  # type: ignore[index]
                for report in source_reports
            },
        },
    )


def write_collection(
    collection: PartnerSourceCollection,
    output_path: Path,
    *,
    report_path: Path | None = None,
) -> PartnerSourceCollection:
    """Write a validated snapshot and report to explicit local paths."""

    _write_collection(collection, Path(output_path), report_path)
    return collection


def _read_input(value: Path | str) -> str:
    if isinstance(value, Path):
        try:
            return value.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise OfficialPartnerSourceError(f"could not read source input: {value}") from exc
    if "<" in value or "var locations" in value:
        return value
    return _read_input(Path(value))


def main(argv: list[str] | None = None) -> int:
    """Run one explicit local source collection command."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("cactus", "bnb", "izi", "statuscard"), required=True)
    parser.add_argument("--html", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.source == "cactus":
            collection = (
                fetch_cactus(args.url or DEFAULT_CACTUS_URL)
                if args.html is None
                else collect_cactus(
                    _read_input(args.html), source_url=args.url or DEFAULT_CACTUS_URL
                )
            )
        elif args.source == "bnb":
            collection = (
                fetch_bnb(args.url or DEFAULT_BNB_URL)
                if args.html is None
                else collect_bnb(_read_input(args.html), source_url=args.url or DEFAULT_BNB_URL)
            )
        elif args.source == "izi":
            if args.html is None:
                collection = fetch_izi(args.url or DEFAULT_IZI_URL)
            else:
                collection = collect_izi(
                    _read_input(args.html), source_url=args.url or DEFAULT_IZI_URL
                )
        else:
            if args.html is None:
                collection = fetch_statuscard(args.url or DEFAULT_STATUS_URL)
            else:
                collection = collect_statuscard(
                    _read_input(args.html), source_url=args.url or DEFAULT_STATUS_URL
                )
        write_collection(collection, args.output, report_path=args.report)
    except OfficialPartnerSourceError as exc:
        print(f"{args.source}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(collection.report["counts"], ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "BNB_CARD_ID",
    "CACTUS_CARD_ID",
    "DEFAULT_BNB_URL",
    "DEFAULT_CACTUS_URL",
    "DEFAULT_IZI_URL",
    "DEFAULT_STATUS_URL",
    "IZI_CARD_ID",
    "STATUS_CARD_ID",
    "TELEGRAM_MAX_MESSAGE_LENGTH",
    "TELEGRAM_SAFE_MESSAGE_LENGTH",
    "OfficialPartnerSourceError",
    "PartnerSourceCollection",
    "collect_bnb",
    "collect_cactus",
    "collect_cactus_pages",
    "collect_izi",
    "collect_statuscard",
    "combine_collections",
    "fetch_bnb",
    "fetch_cactus",
    "fetch_izi",
    "fetch_statuscard",
    "main",
    "write_collection",
]


if __name__ == "__main__":
    raise SystemExit(main())
