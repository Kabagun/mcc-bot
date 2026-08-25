"""Load, validate, and query the editable card moneyback catalog."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

MCC_PATTERN = re.compile(r"(?:mcc[\s:=-]*)?([0-9]{4})", re.IGNORECASE)
SUPPORTED_UNITS = frozenset({"percent", "currency"})
PERCENT_TAX_THRESHOLD = Decimal("2")
PERCENT_TAX_RATE = Decimal("0.13")


class CatalogError(ValueError):
    """Raised when the catalog cannot be loaded or does not follow its contract."""


class InvalidMccError(ValueError):
    """Raised when a user supplied value is not a four-digit MCC code."""


@dataclass(frozen=True, slots=True)
class Moneyback:
    """A moneyback value and the unit in which it is displayed.

    ``percent`` is useful for the usual card cashback-rate data. ``currency``
    represents an absolute amount and requires a three-letter ISO-style code.
    The catalog should use one unit/currency consistently when values are to be
    compared directly.
    """

    value: Decimal
    unit: str = "percent"
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.value < 0:
            raise CatalogError("moneyback must not be negative")
        if self.unit not in SUPPORTED_UNITS:
            raise CatalogError(
                f"moneyback unit must be one of {sorted(SUPPORTED_UNITS)}, got {self.unit!r}"
            )
        if self.unit == "currency" and not self.currency:
            raise CatalogError("currency is required when moneyback unit is 'currency'")
        if self.unit == "percent" and self.currency is not None:
            raise CatalogError("currency is not allowed when moneyback unit is 'percent'")


@dataclass(frozen=True, slots=True)
class OfferDetails:
    """Moneyback metadata shared by explicit and default offers."""

    moneyback: Moneyback
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class CardOffer:
    """One card's moneyback value for one MCC."""

    mcc: str
    moneyback: Moneyback
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class Card:
    """A card and all MCC-specific offers configured for it."""

    id: str
    name: str
    issuer: str | None
    offers: tuple[CardOffer, ...]
    notes: str | None = None
    default_offer: OfferDetails | None = None
    excluded_mccs: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CardMatch:
    """A card returned by an MCC lookup, paired with its matching offer."""

    card: Card
    offer: CardOffer


def calculate_net_moneyback(moneyback: Moneyback) -> Decimal:
    """Return percentage moneyback after the 13% tax above the 2% threshold."""

    if moneyback.unit != "percent" or moneyback.value <= PERCENT_TAX_THRESHOLD:
        return moneyback.value
    return PERCENT_TAX_THRESHOLD + (moneyback.value - PERCENT_TAX_THRESHOLD) * (
        Decimal("1") - PERCENT_TAX_RATE
    )


def normalize_mcc(raw_value: str) -> str:
    """Normalize a raw MCC value to exactly four digits.

    The parser accepts a bare code (``5411``) and friendly forms such as
    ``MCC 5411`` or ``mcc:5411``. It deliberately rejects values with more or
    fewer than four digits so a typo cannot silently return another category.
    """

    if not isinstance(raw_value, str):
        raise InvalidMccError("MCC must be a four-digit number")
    match = MCC_PATTERN.fullmatch(raw_value.strip())
    if match is None:
        raise InvalidMccError("MCC must be a four-digit number, for example 5411")
    return match.group(1)


def _text(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise CatalogError(f"{field} must be a non-empty string")
    result = value.strip()
    if required and not result:
        raise CatalogError(f"{field} must be a non-empty string")
    return result or None


def _decimal(value: Any, field: str) -> Decimal:
    # JSON numbers decode to Python ``int`` or ``float``. Strings are rejected
    # deliberately so a typo such as ``"5%"`` cannot enter the catalog.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CatalogError(f"{field} must be a non-negative JSON number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CatalogError(f"{field} must be a non-negative JSON number") from exc
    if not result.is_finite() or result < 0:
        raise CatalogError(f"{field} must be a non-negative finite number")
    return result


def _moneyback(raw_offer: dict[str, Any], prefix: str) -> Moneyback:
    value = _decimal(raw_offer.get("moneyback"), f"{prefix}.moneyback")
    raw_unit = raw_offer.get("unit", "percent")
    unit = _text(raw_unit, f"{prefix}.unit")
    assert unit is not None
    unit = unit.lower()
    currency = _text(raw_offer.get("currency"), f"{prefix}.currency", required=False)
    if currency is not None:
        currency = currency.upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise CatalogError(f"{prefix}.currency must be a three-letter code")
    try:
        return Moneyback(value=value, unit=unit, currency=currency)
    except CatalogError as exc:
        raise CatalogError(f"{prefix}: {exc}") from exc


def _offer_details(raw_offer: dict[str, Any], prefix: str) -> OfferDetails:
    return OfferDetails(
        moneyback=_moneyback(raw_offer, prefix),
        notes=_text(raw_offer.get("notes"), f"{prefix}.notes", required=False),
    )


def _catalog_mcc(raw_value: Any, field: str) -> str:
    if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int)):
        raise CatalogError(f"{field} must be a four-digit MCC")
    try:
        return normalize_mcc(str(raw_value))
    except InvalidMccError as exc:
        raise CatalogError(f"{field} must be a four-digit MCC") from exc


def _parse_card(raw_card: Any, card_index: int) -> Card:
    if not isinstance(raw_card, dict):
        raise CatalogError(f"cards[{card_index}] must be an object")
    card_id = _text(raw_card.get("id"), f"cards[{card_index}].id")
    name = _text(raw_card.get("name"), f"cards[{card_index}].name")
    issuer = _text(raw_card.get("issuer"), f"cards[{card_index}].issuer", required=False)
    notes = _text(raw_card.get("notes"), f"cards[{card_index}].notes", required=False)
    assert card_id is not None and name is not None
    raw_offers = raw_card.get("offers", [])
    if not isinstance(raw_offers, list):
        raise CatalogError(f"cards[{card_index}].offers must be an array")

    offers: list[CardOffer] = []
    seen_mcc: set[str] = set()
    for offer_index, raw_offer in enumerate(raw_offers):
        if not isinstance(raw_offer, dict):
            raise CatalogError(f"cards[{card_index}].offers[{offer_index}] must be an object")
        prefix = f"cards[{card_index}].offers[{offer_index}]"
        mcc = _catalog_mcc(raw_offer.get("mcc", ""), f"{prefix}.mcc")
        if mcc in seen_mcc:
            raise CatalogError(f"cards[{card_index}] contains duplicate offer for MCC {mcc}")
        seen_mcc.add(mcc)
        details = _offer_details(raw_offer, prefix)
        offers.append(
            CardOffer(
                mcc=mcc,
                moneyback=details.moneyback,
                notes=details.notes,
            )
        )
    raw_default = raw_card.get("default_offer")
    if raw_default is None:
        default_offer = None
    elif not isinstance(raw_default, dict):
        raise CatalogError(f"cards[{card_index}].default_offer must be an object")
    else:
        default_offer = _offer_details(raw_default, f"cards[{card_index}].default_offer")

    raw_excluded = raw_card.get("excluded_mccs", [])
    if not isinstance(raw_excluded, list):
        raise CatalogError(f"cards[{card_index}].excluded_mccs must be an array")
    excluded_mccs: set[str] = set()
    for exclusion_index, raw_mcc in enumerate(raw_excluded):
        field = f"cards[{card_index}].excluded_mccs[{exclusion_index}]"
        mcc = _catalog_mcc(raw_mcc, field)
        if mcc in excluded_mccs:
            raise CatalogError(f"cards[{card_index}] contains duplicate excluded MCC {mcc}")
        excluded_mccs.add(mcc)

    return Card(
        id=card_id,
        name=name,
        issuer=issuer,
        offers=tuple(offers),
        notes=notes,
        default_offer=default_offer,
        excluded_mccs=frozenset(excluded_mccs),
    )


def _moneyback_dimensions_label(dimensions: tuple[str, str | None]) -> str:
    unit, currency = dimensions
    return f"{unit}/{currency}" if currency else unit


def _effective_offer(card: Card, mcc: str) -> CardOffer | None:
    """Resolve one card's explicit/default/excluded offer precedence."""

    for offer in card.offers:
        if offer.mcc == mcc:
            return offer
    if mcc in card.excluded_mccs or card.default_offer is None:
        return None
    return CardOffer(
        mcc=mcc,
        moneyback=card.default_offer.moneyback,
        notes=card.default_offer.notes,
    )


def _validate_moneyback_dimensions(cards: tuple[Card, ...]) -> None:
    """Ensure every effective offer for each possible MCC is comparable."""

    # MCCs are exactly four ASCII digits, so this finite pass also covers
    # default offers whose effective MCCs are not listed explicitly in JSON.
    offer_maps = [(card, {offer.mcc: offer for offer in card.offers}) for card in cards]
    for mcc_number in range(10000):
        mcc = f"{mcc_number:04d}"
        dimensions_by_mcc: dict[str, tuple[tuple[str, str | None], str]] = {}
        for card, offer_map in offer_maps:
            offer = offer_map.get(mcc)
            if offer is None and mcc not in card.excluded_mccs and card.default_offer is not None:
                offer = CardOffer(
                    mcc=mcc,
                    moneyback=card.default_offer.moneyback,
                    notes=card.default_offer.notes,
                )
            if offer is None:
                continue
            dimensions = (offer.moneyback.unit, offer.moneyback.currency)
            previous = dimensions_by_mcc.get(mcc)
            if previous is None:
                dimensions_by_mcc[mcc] = (dimensions, card.id)
                continue
            previous_dimensions, previous_card_id = previous
            if dimensions != previous_dimensions:
                previous_label = _moneyback_dimensions_label(previous_dimensions)
                current_label = _moneyback_dimensions_label(dimensions)
                raise CatalogError(
                    f"MCC {mcc} has incompatible moneyback units: "
                    f"card {previous_card_id!r} uses {previous_label}, "
                    f"card {card.id!r} uses {current_label}"
                )


@dataclass(frozen=True, slots=True)
class CardCatalog:
    """Validated in-memory catalog used by Telegram handlers."""

    cards: tuple[Card, ...]

    @classmethod
    def from_file(cls, path: Path | str) -> CardCatalog:
        """Read and validate a JSON catalog from ``path``."""

        path = Path(path)
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CatalogError(f"Could not read catalog {path}: {exc.strerror or exc}") from exc
        except UnicodeDecodeError as exc:
            raise CatalogError(f"Catalog {path} must be UTF-8 encoded") from exc
        try:
            raw_catalog = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise CatalogError(f"Catalog {path} is not valid JSON: {exc.msg}") from exc
        if not isinstance(raw_catalog, dict):
            raise CatalogError("catalog root must be an object")
        if "version" not in raw_catalog:
            raise CatalogError("catalog.version is required and must be 1")
        version = raw_catalog["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise CatalogError("catalog version must be 1")
        raw_cards = raw_catalog.get("cards")
        if not isinstance(raw_cards, list):
            raise CatalogError("catalog.cards must be an array")

        cards: list[Card] = []
        seen_ids: set[str] = set()
        for card_index, raw_card in enumerate(raw_cards):
            card = _parse_card(raw_card, card_index)
            if card.id in seen_ids:
                raise CatalogError(f"duplicate card id: {card.id}")
            seen_ids.add(card.id)
            cards.append(card)

        # Sorting raw numeric values is only meaningful when all effective
        # offers for a given MCC share the same unit and currency. Reject the
        # ambiguous catalog at startup instead of returning misleading rankings.
        _validate_moneyback_dimensions(tuple(cards))
        return cls(cards=tuple(cards))

    def lookup(self, raw_mcc: str) -> tuple[CardMatch, ...]:
        """Return matching cards sorted by moneyback descending.

        Card name and ID provide deterministic tie-breakers. Sorting compares
        the numeric values in the catalog; for meaningful comparisons, keep a
        catalog (or a queried subset) on one unit and currency.
        """

        mcc = normalize_mcc(raw_mcc)
        matches: list[CardMatch] = []
        for card in self.cards:
            offer = _effective_offer(card, mcc)
            if offer is not None:
                matches.append(CardMatch(card=card, offer=offer))
        matches.sort(
            key=lambda match: (
                -match.offer.moneyback.value,
                match.card.name.casefold(),
                match.card.id,
            )
        )
        return tuple(matches)
