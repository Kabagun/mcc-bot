"""Russian user-facing formatting for MCC lookup results."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal

from .catalog import (
    PERCENT_TAX_THRESHOLD,
    CardMatch,
    RewardComponent,
    RewardProgram,
    calculate_net_percent,
)
from .descriptions import DescriptionCatalog

MAX_TELEGRAM_MESSAGE_LENGTH = 4096
SAFE_MESSAGE_LENGTH = 3900


def _format_decimal(value: Decimal, *, places: int | None = None) -> str:
    """Render a Decimal with comma decimals and no insignificant zeros."""

    if places is not None:
        quantum = Decimal(1).scaleb(-places)
        value = value.quantize(quantum, rounding=ROUND_HALF_UP)
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return (rendered or "0").replace(".", ",")


def _component_label(component: RewardComponent, *, show_kind: bool = False) -> str:
    if component.unit == "currency":
        currency = component.currency or ""
        return f"{_format_decimal(component.gross_value)} {currency}".strip()
    value = _format_decimal(component.gross_value)
    kind_label = ""
    if show_kind:
        kind_label = " деньгами" if component.kind == "cash" else " баллами"
    result = f"{value}%{kind_label}"
    if not component.tax_exempt and component.gross_value > PERCENT_TAX_THRESHOLD:
        net = _format_decimal(calculate_net_percent(component.gross_value), places=2)
        result += f" ({net}% после налога)"
    return result


def format_moneyback(match: CardMatch) -> str:
    """Render a compact reward, showing kinds only when they disambiguate a stack."""

    if len(match.components) == 1:
        component = match.components[0]
        return _component_label(component, show_kind=component.kind == "points")
    return " + ".join(
        _component_label(component, show_kind=component.kind == "points")
        for component in match.components
    )


def _format_maximum_reward(program: RewardProgram) -> str:
    maximum = program.maximum_reward
    if maximum is None:
        return "макс. в месяц не указан"
    if maximum.unlimited:
        return "макс. в месяц без лимита"
    assert maximum.amount is not None
    amount = _format_decimal(maximum.amount)
    if maximum.unit == "points":
        return f"макс. в месяц {amount} баллов"
    currency = maximum.currency or ""
    return f"макс. в месяц {amount} {currency}".strip()


def _format_program_terms(program: RewardProgram) -> str:
    minimum = program.minimum_payment
    if minimum is None:
        minimum_label = "мин. платёж не указан"
    else:
        amount = _format_decimal(minimum.amount)
        minimum_label = f"мин. платёж {amount} {minimum.currency}"
    return f"{minimum_label} · {_format_maximum_reward(program)}"


def _match_detail_lines(match: CardMatch) -> tuple[str, ...]:
    lines: list[str] = []
    if match.card.issuer:
        lines.append(f"   🏦 {match.card.issuer}")

    programs = {program.id: program for program in match.card.reward_programs}
    stacked = len(match.components) > 1
    for component in match.components:
        program = programs[component.program_id]
        icon = "⭐" if component.kind == "points" else "💵"
        kind = "Баллами: " if component.kind == "points" else "Деньгами: "
        prefix = kind if stacked else ""
        lines.append(f"   {icon} {prefix}{_format_program_terms(program)}")
    return tuple(lines)


def _description(descriptions: DescriptionCatalog | Mapping[str, str] | None, mcc: str) -> str:
    if descriptions is None:
        return "описание не найдено"
    if isinstance(descriptions, DescriptionCatalog):
        return descriptions.get(mcc)
    return descriptions.get(mcc, "описание не найдено")


def _header_emoji(description: str) -> str:
    lowered = description.casefold()
    if any(word in lowered for word in ("продукт", "бакал", "супермаркет", "еда")):
        return "🛒"
    if any(word in lowered for word in ("медиц", "здоров", "аптек", "стомат", "ветерин")):
        return "🩺"
    if any(word in lowered for word in ("ресторан", "кафе", "питани", "бар")):  # noqa: RUF001
        return "🍽️"
    if any(word in lowered for word in ("транспорт", "авиал", "такси", "автобус")):
        return "🚕"
    if any(word in lowered for word in ("одежд", "обув", "магазин")):
        return "🛍️"
    return "🧾"


def format_matches(
    mcc: str,
    matches: tuple[CardMatch, ...],
    descriptions: DescriptionCatalog | Mapping[str, str] | None = None,
) -> str:
    """Format an MCC lookup as a compact Russian Telegram message."""

    description = _description(descriptions, mcc)
    header = f"{_header_emoji(description)} MCC {mcc} — {description}"
    if not matches:
        return f"{header}\n\n❌ Доступных карт нет."
    lines = [header, ""]
    for index, match in enumerate(matches, start=1):
        lines.append(f"{index}. {match.card.emoji} {match.card.name} — {format_moneyback(match)}")
        lines.extend(_match_detail_lines(match))
    return "\n".join(lines)


def split_message(message: str, *, max_length: int = SAFE_MESSAGE_LENGTH) -> tuple[str, ...]:
    """Split long output at line boundaries accepted by Telegram."""

    if len(message) <= max_length:
        return (message,)
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in message.splitlines():
        line_length = len(line) + (1 if current else 0)
        if current and current_length + line_length > max_length:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
            line_length = len(line)
        if len(line) > max_length:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_length = 0
            for offset in range(0, len(line), max_length):
                chunks.append(line[offset : offset + max_length])
            continue
        current.append(line)
        current_length += line_length
    if current:
        chunks.append("\n".join(current))
    return tuple(chunks)
