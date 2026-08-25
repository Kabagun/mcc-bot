"""User-facing formatting for MCC lookup results."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .catalog import PERCENT_TAX_THRESHOLD, CardMatch, calculate_net_moneyback

MAX_TELEGRAM_MESSAGE_LENGTH = 4096
SAFE_MESSAGE_LENGTH = 3900


def _format_decimal(value: Decimal, *, places: int | None = None) -> str:
    """Render a Decimal without exponent notation or insignificant zeros."""

    if places is not None:
        quantum = Decimal(1).scaleb(-places)
        value = value.quantize(quantum, rounding=ROUND_HALF_UP)
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def format_moneyback(match: CardMatch) -> str:
    """Render one moneyback value using the unit configured for its offer."""

    moneyback = match.offer.moneyback
    value = _format_decimal(moneyback.value)
    if moneyback.unit == "percent":
        if moneyback.value > PERCENT_TAX_THRESHOLD:
            net_value = _format_decimal(calculate_net_moneyback(moneyback), places=2)
            return f"{value}% ({net_value}% after tax)"
        return f"{value}%"
    return f"{value} {moneyback.currency}"


def format_matches(mcc: str, matches: tuple[CardMatch, ...]) -> str:
    """Format an MCC lookup as a concise Telegram message."""

    if not matches:
        return f"No cards found for MCC {mcc}."
    lines = [f"Cards for MCC {mcc} (highest moneyback first):"]
    for index, match in enumerate(matches, start=1):
        issuer = f" — {match.card.issuer}" if match.card.issuer else ""
        lines.append(f"{index}. {match.card.name}{issuer}: {format_moneyback(match)}")
        if match.card.notes:
            lines.append(f"   Note: {match.card.notes}")
        if match.offer.notes:
            lines.append(f"   Offer note: {match.offer.notes}")
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
