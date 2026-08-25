"""Load, validate, and query the versioned card rewards catalog.

The JSON catalog deliberately keeps card presentation, eligibility conditions,
and reward programs separate. A card may have more than one independent
program (for example cash plus points), while each program resolves its own
explicit MCC rules, exclusions, and fallback in that order.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

MCC_PATTERN = re.compile(r"(?:mcc[\s:=-]*)?([0-9]{4})", re.IGNORECASE)
SUPPORTED_UNITS = frozenset({"percent", "currency"})
SUPPORTED_PROGRAM_KINDS = frozenset({"cash", "points"})
SUPPORTED_REWARD_CAP_UNITS = frozenset({"currency", "points"})
PERCENT_TAX_THRESHOLD = Decimal("2")
PERCENT_TAX_RATE = Decimal("0.13")


class CatalogError(ValueError):
    """Raised when the catalog cannot be loaded or violates its contract."""


class InvalidMccError(ValueError):
    """Raised when a user supplied value is not a four-digit MCC code."""


@dataclass(frozen=True, slots=True)
class Moneyback:
    """A percentage reward value.

    This small value object is retained as a convenient public API for callers
    that need to inspect a component. Production data uses ``percent``;
    ``currency`` remains available for comparable absolute rewards.
    """

    value: Decimal
    unit: str = "percent"
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.value < 0:
            raise CatalogError("Манибэк не может быть отрицательным")
        if self.unit not in SUPPORTED_UNITS:
            raise CatalogError(
                f"Единица манибэка должна быть одной из {sorted(SUPPORTED_UNITS)}, "
                f"получено {self.unit!r}"
            )
        if self.unit == "currency" and not self.currency:
            raise CatalogError("Для единицы currency требуется код валюты")
        if self.unit == "percent" and self.currency is not None:
            raise CatalogError("Код валюты нельзя указывать для единицы percent")


@dataclass(frozen=True, slots=True)
class CardCondition:
    """A typed, renderable card eligibility condition."""

    kind: str
    count: int | None = None
    amount: Decimal | None = None
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class MoneyAmount:
    """A non-negative monetary amount in a three-letter currency."""

    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class RewardCap:
    """A maximum monthly reward amount, or an explicitly unlimited reward."""

    amount: Decimal | None
    unit: str
    currency: str | None = None

    @property
    def unlimited(self) -> bool:
        """Return whether the reward has no configured maximum."""

        return self.amount is None


@dataclass(frozen=True, slots=True)
class RewardOffer:
    """One explicit MCC value within a reward program."""

    mcc: str
    value: Decimal
    unit: str = "percent"
    currency: str | None = None

    @property
    def moneyback(self) -> Moneyback:
        """Expose the value through the v1-compatible object shape."""

        return Moneyback(self.value, unit=self.unit, currency=self.currency)


@dataclass(frozen=True, slots=True)
class RewardProgram:
    """One independently resolved cash or points program."""

    id: str
    kind: str
    tax_exempt: bool
    offers: tuple[RewardOffer, ...]
    default_value: Decimal | None
    excluded_mccs: frozenset[str]
    minimum_payment: MoneyAmount | None
    maximum_reward: RewardCap | None
    unit: str = "percent"
    currency: str | None = None

    def offer_map(self) -> dict[str, RewardOffer]:
        """Return explicit offers indexed by normalized MCC."""

        return {offer.mcc: offer for offer in self.offers}


@dataclass(frozen=True, slots=True)
class Card:
    """A card, its display metadata, conditions, and reward programs."""

    id: str
    name: str
    issuer: str | None
    emoji: str
    condition: CardCondition | None
    reward_programs: tuple[RewardProgram, ...]

    @property
    def offers(self) -> tuple[RewardOffer, ...]:
        """Return all explicit offers flattened across programs."""

        return tuple(offer for program in self.reward_programs for offer in program.offers)


@dataclass(frozen=True, slots=True)
class RewardComponent:
    """One resolved reward component in a card match."""

    program_id: str
    kind: str
    gross_value: Decimal
    tax_exempt: bool
    unit: str = "percent"
    currency: str | None = None

    @property
    def gross_percent(self) -> Decimal:
        """Compatibility alias for percentage production data."""

        return self.gross_value

    @property
    def net_percent(self) -> Decimal:
        """Return this component after the percentage tax rule."""

        if self.unit != "percent":
            return self.gross_value
        return calculate_net_percent(self.gross_value, tax_exempt=self.tax_exempt)

    @property
    def net_value(self) -> Decimal:
        """Return this component's net value in its configured unit."""

        return self.net_percent

    @property
    def moneyback(self) -> Moneyback:
        """Expose the gross value through a familiar object shape."""

        return Moneyback(self.gross_value, unit=self.unit, currency=self.currency)


@dataclass(frozen=True, slots=True)
class CardMatch:
    """A card and all reward components effective for one MCC."""

    card: Card
    mcc: str
    components: tuple[RewardComponent, ...]

    @property
    def gross_percent(self) -> Decimal:
        """Compatibility alias for the sum used for ranking."""

        return sum((component.gross_percent for component in self.components), Decimal("0"))

    @property
    def gross_value(self) -> Decimal:
        """Sum of gross numeric component values used for ranking."""

        return self.gross_percent

    @property
    def net_percent(self) -> Decimal:
        """Compatibility alias for the component-wise net sum."""

        return sum((component.net_percent for component in self.components), Decimal("0"))

    @property
    def net_value(self) -> Decimal:
        """Sum of component-wise net numeric values."""

        return self.net_percent

    @property
    def offer(self) -> RewardOffer:
        """Return the first component as a migration convenience."""

        if not self.components:
            raise AttributeError("a card match has no reward components")
        component = self.components[0]
        return RewardOffer(self.mcc, component.gross_value, component.unit, component.currency)


def calculate_net_percent(value: Decimal, *, tax_exempt: bool = False) -> Decimal:
    """Apply 13% tax to the portion above 2%, unless explicitly exempt."""

    if tax_exempt or value <= PERCENT_TAX_THRESHOLD:
        return value
    return PERCENT_TAX_THRESHOLD + (value - PERCENT_TAX_THRESHOLD) * (
        Decimal("1") - PERCENT_TAX_RATE
    )


def calculate_net_moneyback(moneyback: Moneyback, *, tax_exempt: bool = False) -> Decimal:
    """Compatibility wrapper applying the v2 component tax rule."""

    if moneyback.unit != "percent":
        return moneyback.value
    return calculate_net_percent(moneyback.value, tax_exempt=tax_exempt)


def normalize_mcc(raw_value: str) -> str:
    """Normalize a raw MCC value to exactly four digits."""

    if not isinstance(raw_value, str):
        raise InvalidMccError("MCC должен состоять из четырёх цифр")
    match = MCC_PATTERN.fullmatch(raw_value.strip())
    if match is None:
        raise InvalidMccError("MCC должен состоять из четырёх цифр, например 5411")
    return match.group(1)


def _text(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise CatalogError(f"{field} должен быть непустой строкой")
    result = value.strip()
    if required and not result:
        raise CatalogError(f"{field} должен быть непустой строкой")
    return result or None


def _reject_unknown(raw: dict[str, Any], allowed: set[str], prefix: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise CatalogError(f"{prefix} содержит неподдерживаемые поля: {', '.join(unknown)}")


def _decimal(value: Any, field: str) -> Decimal:
    """Parse only JSON numeric values; reject strings and booleans."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CatalogError(f"{field} должен быть неотрицательным числом JSON")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CatalogError(f"{field} должен быть неотрицательным числом JSON") from exc
    if not result.is_finite() or result < 0:
        raise CatalogError(f"{field} должен быть конечным неотрицательным числом")
    return result


def _catalog_mcc(raw_value: Any, field: str) -> str:
    if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int)):
        raise CatalogError(f"{field} должен быть четырёхзначным MCC")
    try:
        return normalize_mcc(str(raw_value))
    except InvalidMccError as exc:
        raise CatalogError(f"{field} должен быть четырёхзначным MCC") from exc


def _parse_condition(raw_condition: Any, prefix: str) -> CardCondition | None:
    if raw_condition is None:
        return None
    if not isinstance(raw_condition, dict):
        raise CatalogError(f"{prefix} должен быть объектом")
    _reject_unknown(raw_condition, {"kind", "count", "amount", "currency"}, prefix)
    kind = _text(raw_condition.get("kind"), f"{prefix}.kind")
    assert kind is not None
    if kind == "placeholder_name":
        return CardCondition(kind=kind)
    if kind == "max_connected_categories":
        count = raw_condition.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise CatalogError(f"{prefix}.count должен быть положительным целым числом")
        return CardCondition(kind=kind, count=count)
    if kind == "selected_category":
        return CardCondition(kind=kind)
    if kind == "minimum_spend":
        amount = _decimal(raw_condition.get("amount"), f"{prefix}.amount")
        currency = _text(raw_condition.get("currency"), f"{prefix}.currency")
        assert currency is not None
        return CardCondition(kind=kind, amount=amount, currency=currency.upper())
    if kind == "kufar_rules":
        return CardCondition(kind=kind)
    raise CatalogError(f"{prefix}.kind не поддерживается: {kind!r}")


def _parse_exclusions(raw_value: Any, prefix: str) -> frozenset[str]:
    if raw_value is None:
        return frozenset()
    if not isinstance(raw_value, list):
        raise CatalogError(f"{prefix} должен быть массивом")
    result: set[str] = set()
    for index, raw_mcc in enumerate(raw_value):
        field = f"{prefix}[{index}]"
        mcc = _catalog_mcc(raw_mcc, field)
        if mcc in result:
            raise CatalogError(f"{prefix} содержит дублирующий MCC {mcc}")
        result.add(mcc)
    return frozenset(result)


def _currency(value: Any, field: str) -> str:
    currency = _text(value, field)
    assert currency is not None
    currency = currency.upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise CatalogError(f"{field} должен быть трёхбуквенным кодом")
    return currency


def _parse_minimum_payment(raw_value: Any, prefix: str) -> MoneyAmount | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise CatalogError(f"{prefix} должен быть объектом")
    _reject_unknown(raw_value, {"amount", "currency"}, prefix)
    return MoneyAmount(
        amount=_decimal(raw_value.get("amount"), f"{prefix}.amount"),
        currency=_currency(raw_value.get("currency"), f"{prefix}.currency"),
    )


def _parse_maximum_reward(raw_value: Any, prefix: str) -> RewardCap | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise CatalogError(f"{prefix} должен быть объектом")
    _reject_unknown(raw_value, {"amount", "unlimited", "unit", "currency"}, prefix)
    unit = _text(raw_value.get("unit"), f"{prefix}.unit")
    assert unit is not None
    unit = unit.lower()
    if unit not in SUPPORTED_REWARD_CAP_UNITS:
        raise CatalogError(
            f"{prefix}.unit должен быть одним из {sorted(SUPPORTED_REWARD_CAP_UNITS)}"
        )
    has_unlimited = "unlimited" in raw_value
    unlimited = raw_value.get("unlimited", False)
    if not isinstance(unlimited, bool):
        raise CatalogError(f"{prefix}.unlimited должен быть boolean")
    has_amount = "amount" in raw_value
    if has_amount == has_unlimited or (has_unlimited and not unlimited):
        raise CatalogError(f"{prefix} должен содержать amount или unlimited: true")
    amount = None if unlimited else _decimal(raw_value["amount"], f"{prefix}.amount")
    raw_currency = raw_value.get("currency")
    if unit == "currency":
        currency = _currency(raw_currency, f"{prefix}.currency")
    else:
        if raw_currency is not None:
            raise CatalogError(f"{prefix}.currency запрещён для unit points")
        currency = None
    return RewardCap(amount=amount, unit=unit, currency=currency)


def _parse_reward_program(raw_program: Any, card_index: int, program_index: int) -> RewardProgram:
    prefix = f"cards[{card_index}].reward_programs[{program_index}]"
    if not isinstance(raw_program, dict):
        raise CatalogError(f"{prefix} должен быть объектом")
    _reject_unknown(
        raw_program,
        {
            "id",
            "kind",
            "tax_exempt",
            "unit",
            "currency",
            "offers",
            "rules",
            "default",
            "excluded_mccs",
            "minimum_payment",
            "maximum_reward",
        },
        prefix,
    )
    kind = _text(raw_program.get("kind"), f"{prefix}.kind")
    assert kind is not None
    kind = kind.lower()
    if kind not in SUPPORTED_PROGRAM_KINDS:
        raise CatalogError(f"{prefix}.kind должен быть cash или points")
    program_id = _text(raw_program.get("id", kind), f"{prefix}.id")
    assert program_id is not None
    tax_exempt = raw_program.get("tax_exempt")
    if not isinstance(tax_exempt, bool):
        raise CatalogError(f"{prefix}.tax_exempt должен быть boolean")
    unit = _text(raw_program.get("unit", "percent"), f"{prefix}.unit")
    assert unit is not None
    unit = unit.lower()
    if unit not in SUPPORTED_UNITS:
        raise CatalogError(f"{prefix}.unit должен быть percent или currency")
    currency = _text(raw_program.get("currency"), f"{prefix}.currency", required=False)
    if currency is not None:
        currency = currency.upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise CatalogError(f"{prefix}.currency должен быть трёхбуквенным кодом")
    if unit == "currency" and currency is None:
        raise CatalogError(f"{prefix}.currency обязателен для unit currency")
    if unit == "percent" and currency is not None:
        raise CatalogError(f"{prefix}.currency запрещён для unit percent")

    raw_offers = raw_program.get("offers", [])
    if not isinstance(raw_offers, list):
        raise CatalogError(f"{prefix}.offers должен быть массивом")
    raw_rules = raw_program.get("rules", [])
    if not isinstance(raw_rules, list):
        raise CatalogError(f"{prefix}.rules должен быть массивом")
    offers: list[RewardOffer] = []
    seen_mcc: set[str] = set()

    def add_offer(
        raw_offer: Any, offer_prefix: str, mcc_value: Any, *, validate_fields: bool = True
    ) -> None:
        if not isinstance(raw_offer, dict):
            raise CatalogError(f"{offer_prefix} должен быть объектом")
        if validate_fields:
            _reject_unknown(raw_offer, {"mcc", "value"}, offer_prefix)
        mcc = _catalog_mcc(mcc_value, f"{offer_prefix}.mcc")
        if mcc in seen_mcc:
            raise CatalogError(f"{prefix} содержит дублирующий offer для MCC {mcc}")
        seen_mcc.add(mcc)
        offers.append(
            RewardOffer(
                mcc=mcc,
                value=_decimal(raw_offer.get("value"), f"{offer_prefix}.value"),
                unit=unit,
                currency=currency,
            )
        )

    for offer_index, raw_offer in enumerate(raw_offers):
        if not isinstance(raw_offer, dict):
            raise CatalogError(f"{prefix}.offers[{offer_index}] должен быть объектом")
        add_offer(raw_offer, f"{prefix}.offers[{offer_index}]", raw_offer.get("mcc", ""))

    for rule_index, raw_rule in enumerate(raw_rules):
        rule_prefix = f"{prefix}.rules[{rule_index}]"
        if not isinstance(raw_rule, dict):
            raise CatalogError(f"{rule_prefix} должен быть объектом")
        _reject_unknown(raw_rule, {"mccs", "value"}, rule_prefix)
        raw_mccs = raw_rule.get("mccs")
        if not isinstance(raw_mccs, list) or not raw_mccs:
            raise CatalogError(f"{rule_prefix}.mccs должен быть непустым массивом")
        for mcc_index, raw_mcc in enumerate(raw_mccs):
            # Grouped rules are normalized to explicit values in memory. A
            # duplicate across groups is rejected just like duplicate offers.
            add_offer(
                raw_rule,
                f"{rule_prefix}.mccs[{mcc_index}]",
                raw_mcc,
                validate_fields=False,
            )

    raw_default = raw_program.get("default")
    if raw_default is None:
        default_value = None
    elif not isinstance(raw_default, dict):
        raise CatalogError(f"{prefix}.default должен быть объектом")
    else:
        _reject_unknown(raw_default, {"value"}, f"{prefix}.default")
        default_value = _decimal(raw_default.get("value"), f"{prefix}.default.value")
    excluded_mccs = _parse_exclusions(
        raw_program.get("excluded_mccs", []), f"{prefix}.excluded_mccs"
    )
    minimum_payment = _parse_minimum_payment(
        raw_program.get("minimum_payment"), f"{prefix}.minimum_payment"
    )
    maximum_reward = _parse_maximum_reward(
        raw_program.get("maximum_reward"), f"{prefix}.maximum_reward"
    )
    return RewardProgram(
        id=program_id,
        kind=kind,
        tax_exempt=tax_exempt,
        offers=tuple(offers),
        default_value=default_value,
        excluded_mccs=excluded_mccs,
        minimum_payment=minimum_payment,
        maximum_reward=maximum_reward,
        unit=unit,
        currency=currency,
    )


def _parse_card(raw_card: Any, card_index: int) -> Card:
    prefix = f"cards[{card_index}]"
    if not isinstance(raw_card, dict):
        raise CatalogError(f"{prefix} должен быть объектом")
    _reject_unknown(
        raw_card, {"id", "name", "issuer", "emoji", "condition", "reward_programs"}, prefix
    )
    card_id = _text(raw_card.get("id"), f"{prefix}.id")
    name = _text(raw_card.get("name"), f"{prefix}.name")
    issuer = _text(raw_card.get("issuer"), f"{prefix}.issuer", required=False)
    emoji = _text(raw_card.get("emoji"), f"{prefix}.emoji")
    assert card_id is not None and name is not None and emoji is not None
    condition = _parse_condition(raw_card.get("condition"), f"{prefix}.condition")
    raw_programs = raw_card.get("reward_programs")
    if not isinstance(raw_programs, list) or not raw_programs:
        raise CatalogError(f"{prefix}.reward_programs должен быть непустым массивом")
    programs: list[RewardProgram] = []
    seen_program_ids: set[str] = set()
    for program_index, raw_program in enumerate(raw_programs):
        program = _parse_reward_program(raw_program, card_index, program_index)
        if program.id in seen_program_ids:
            raise CatalogError(f"{prefix} содержит дублирующую программу {program.id!r}")
        seen_program_ids.add(program.id)
        programs.append(program)
    return Card(
        id=card_id,
        name=name,
        issuer=issuer,
        emoji=emoji,
        condition=condition,
        reward_programs=tuple(programs),
    )


def _effective_program_offer(program: RewardProgram, mcc: str) -> RewardOffer | None:
    """Resolve explicit > exclusion > default for one program."""

    explicit = program.offer_map().get(mcc)
    if explicit is not None:
        return explicit
    if mcc in program.excluded_mccs or program.default_value is None:
        return None
    return RewardOffer(
        mcc=mcc,
        value=program.default_value,
        unit=program.unit,
        currency=program.currency,
    )


def _validate_reward_dimensions(cards: tuple[Card, ...]) -> None:
    """Reject cross-card unit mismatches before ranking results."""

    # Four-digit MCC space is small. The finite pass also checks defaults that
    # do not appear explicitly in the JSON and preserves the v1 safety rule.
    for mcc_number in range(10000):
        mcc = f"{mcc_number:04d}"
        dimensions: tuple[str, str | None] | None = None
        owner: tuple[str, str] | None = None
        for card in cards:
            for program in card.reward_programs:
                if _effective_program_offer(program, mcc) is None:
                    continue
                current = (program.unit, program.currency)
                if dimensions is None:
                    dimensions = current
                    owner = (card.id, program.id)
                    continue
                if current != dimensions:
                    assert owner is not None
                    raise CatalogError(
                        f"MCC {mcc}: единицы манибэка нельзя сравнить: "
                        f"карта {owner[0]!r}, программа {owner[1]!r} использует "
                        f"{dimensions[0]}/{dimensions[1]}, "
                        f"карта {card.id!r}, программа {program.id!r} использует "
                        f"{current[0]}/{current[1]}"
                    )


@dataclass(frozen=True, slots=True)
class CardCatalog:
    """Validated in-memory catalog used by Telegram handlers and the CLI."""

    cards: tuple[Card, ...]

    @classmethod
    def from_file(cls, path: Path | str) -> CardCatalog:
        """Read and validate a UTF-8 version 2 JSON catalog from ``path``."""

        path = Path(path)
        try:
            raw_text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise CatalogError(f"Каталог {path} должен быть в кодировке UTF-8") from exc
        except OSError as exc:
            raise CatalogError(
                f"Не удалось прочитать каталог {path}: {exc.strerror or exc}"  # noqa: RUF001
            ) from exc
        try:
            raw_catalog = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise CatalogError(f"Каталог {path} содержит некорректный JSON: {exc.msg}") from exc
        if not isinstance(raw_catalog, dict):
            raise CatalogError("Корень каталога должен быть объектом")
        _reject_unknown(raw_catalog, {"version", "cards"}, "catalog")
        if "version" not in raw_catalog:
            raise CatalogError("В каталоге обязательно поле version: 2")  # noqa: RUF001
        version = raw_catalog["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version != 2:
            raise CatalogError("Версия каталога должна быть равна 2")
        raw_cards = raw_catalog.get("cards")
        if not isinstance(raw_cards, list):
            raise CatalogError("Поле cards каталога должно быть массивом")

        cards: list[Card] = []
        seen_ids: set[str] = set()
        for card_index, raw_card in enumerate(raw_cards):
            card = _parse_card(raw_card, card_index)
            if card.id in seen_ids:
                raise CatalogError(f"Повторяется идентификатор карты: {card.id}")
            seen_ids.add(card.id)
            cards.append(card)
        card_tuple = tuple(cards)
        _validate_reward_dimensions(card_tuple)
        return cls(cards=card_tuple)

    def lookup(self, raw_mcc: str) -> tuple[CardMatch, ...]:
        """Return cards with at least one component, sorted by gross sum."""

        mcc = normalize_mcc(raw_mcc)
        matches: list[CardMatch] = []
        for card in self.cards:
            components: list[RewardComponent] = []
            for program in card.reward_programs:
                offer = _effective_program_offer(program, mcc)
                if offer is None:
                    continue
                components.append(
                    RewardComponent(
                        program_id=program.id,
                        kind=program.kind,
                        gross_value=offer.value,
                        tax_exempt=program.tax_exempt,
                        unit=offer.unit,
                        currency=offer.currency,
                    )
                )
            if components:
                matches.append(CardMatch(card=card, mcc=mcc, components=tuple(components)))
        matches.sort(
            key=lambda match: (-match.gross_percent, match.card.name.casefold(), match.card.id)
        )
        return tuple(matches)
