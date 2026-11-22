"""Published cashback rules for payments through ERIP."""

# Russian product names and copy are intentional.
# ruff: noqa: RUF001

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from .resources import default_data_path
from .stores import StoreRepository, normalize_store_name

KIND = "erip_rule"
EXCLUSION_CHOICES = (
    "Банковские, финансовые услуги",
    "Мобильная связь",
    "Налоги",
    "Лотереи и казино",
)
MAX_EXCLUSIONS = 8
MAX_BRANCH = 85
_FIELDS = {"card", "issuer", "rate", "reward_kind", "payment_channel"}
_OPTIONAL = {"rule_id", "revision", "exclusions", "limits", "source_url"}


class EripError(ValueError):
    """An invalid or superseded ERIP rule."""


@dataclass(frozen=True, slots=True)
class EripRule:
    id: int
    revision: int
    values: dict[str, Any]

    def edit_values(self) -> dict[str, Any]:
        return {**self.values, "rule_id": self.id, "revision": self.revision}


def _text(value: Any, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise EripError("Введите текст одной строкой.")
    value = " ".join(value.split())
    if len(value) > maximum or (required and not value):
        raise EripError(f"Введите текст длиной от 1 до {maximum} символов.")
    return value


def parse_rate(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,3}(?:[,.][0-9]{1,3})?", value):
        raise EripError("Введите процент от 0 до 100. Например, 1 или 0,5.")
    rate = Decimal(value.replace(",", "."))
    if rate > 100:
        raise EripError("Введите процент от 0 до 100.")
    return format(rate.normalize(), "f")


def normalize_branch(value: Any) -> str:
    branch = _text(value, MAX_BRANCH, required=True)
    return next(
        (item for item in EXCLUSION_CHOICES if item.casefold() == branch.casefold()), branch
    )


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload.keys() >= _FIELDS or payload.keys() - _FIELDS - _OPTIONAL:
        raise EripError("Заполните карту, банк, ставку, вид возврата и условия оплаты.")
    values = {
        "card": _text(payload["card"], 90, required=True),
        "issuer": _text(payload["issuer"], 90, required=True),
        "rate": parse_rate(payload["rate"]),
        "reward_kind": payload["reward_kind"],
        "payment_channel": _text(payload["payment_channel"], 450, required=True),
        "limits": _text(payload.get("limits", ""), 300),
    }
    if values["reward_kind"] not in ("cash", "points"):
        raise EripError("Укажите, начисляется возврат деньгами или баллами.")
    if not all(normalize_store_name(values[key]) for key in ("card", "issuer")):
        raise EripError("Введите название карты и банка.")
    branches = payload.get("exclusions", [])
    if not isinstance(branches, list) or len(branches) > MAX_EXCLUSIONS:
        raise EripError(f"Можно указать до {MAX_EXCLUSIONS} веток ЕРИП.")
    normalized = (normalize_branch(value) for value in branches)
    branches_by_key = {branch.casefold(): branch for branch in normalized}
    values["exclusions"] = sorted(branches_by_key.values(), key=str.casefold)
    url = payload.get("source_url", "")
    if (
        not isinstance(url, str)
        or len(url) > 1024
        or any(char.isspace() or ord(char) < 32 or char in "<>\x7f" for char in url)
    ):
        raise EripError("Отправьте полную ссылку на условия банка.")
    if url:
        try:
            parsed = urlparse(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
            ):
                raise ValueError
        except ValueError:
            raise EripError("Отправьте полную ссылку с http:// или https://.") from None
    values["source_url"] = url
    if "rule_id" in payload or "revision" in payload:
        for key in ("rule_id", "revision"):
            value = payload.get(key)
            if type(value) is not int or not 0 < value < 2**63:
                raise EripError("Откройте текущую карточку ЕРИП и повторите поправку.")
            values[key] = value
    return values


def reward_text(values: dict[str, Any]) -> str:
    if values["rate"] == "0":
        return (
            "Баллы не начисляются"
            if values["reward_kind"] == "points"
            else "Возврат не начисляется"
        )
    unit = "баллами" if values["reward_kind"] == "points" else "деньгами"
    return f"{values['rate'].replace('.', ',')}% {unit}"


def card_title(values: dict[str, Any]) -> str:
    return f"{values['card']} · {values['issuer']}"


def describe(values: dict[str, Any], *, include_source: bool = False) -> str:
    lines = [card_title(values), "", reward_text(values), values["payment_channel"]]
    if values["limits"]:
        lines.append(values["limits"])
    if values["exclusions"]:
        lines.extend(
            (
                "",
                "Возврат не начисляется в этих ветках",
                *(f"• {branch}" for branch in values["exclusions"]),
            )
        )
    if include_source and values["source_url"]:
        lines.extend(("", f"Источник — {values['source_url']}"))
    return "\n".join(lines)


def _key(values: dict[str, Any]) -> str:
    return "|".join(normalize_store_name(values[field]) for field in ("issuer", "card"))


def _row(row: sqlite3.Row) -> EripRule:
    return EripRule(row["id"], row["revision"], json.loads(row["data"]))


def _encoded(values: dict[str, Any]) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True)


class EripRepository:
    """Store confirmed rules in the same transaction as community decisions."""

    def __init__(self, stores: StoreRepository) -> None:
        self.stores = stores

    def initialize(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS erip_rules (
                id INTEGER PRIMARY KEY, card_key TEXT NOT NULL UNIQUE,
                revision INTEGER NOT NULL DEFAULT 1, data TEXT NOT NULL)"""
        )
        seed = json.loads(default_data_path("erip_rules.json").read_text(encoding="utf-8"))
        for item in seed:
            values = validate(item)
            connection.execute(
                "INSERT OR IGNORE INTO erip_rules(card_key,data) VALUES(?,?)",
                (_key(values), _encoded(values)),
            )

    def list_rules(self) -> tuple[EripRule, ...]:
        with self.stores.connection() as connection:
            rows = connection.execute("SELECT * FROM erip_rules ORDER BY card_key").fetchall()
            return tuple(_row(row) for row in rows)

    def get(self, rule_id: int, *, connection: sqlite3.Connection | None = None) -> EripRule | None:
        if connection is None:
            with self.stores.connection() as current:
                return self.get(rule_id, connection=current)
        row = connection.execute("SELECT * FROM erip_rules WHERE id=?", (rule_id,)).fetchone()
        return _row(row) if row is not None else None

    def check(self, connection: sqlite3.Connection, payload: dict[str, Any]) -> None:
        values = {
            key: value for key, value in payload.items() if key not in {"rule_id", "revision"}
        }
        if "rule_id" in payload:
            rule = self.get(payload["rule_id"], connection=connection)
            if rule is None or rule.revision != payload["revision"]:
                raise EripError("Условия ЕРИП уже изменились. Откройте текущую карточку.")
            if (values["card"], values["issuer"]) != (rule.values["card"], rule.values["issuer"]):
                raise EripError("Карта в поправке должна совпадать с выбранной карточкой.")
            if rule.values == values:
                raise EripError("Условия ЕРИП уже указаны без изменений.")
        elif connection.execute(
            "SELECT 1 FROM erip_rules WHERE card_key=?", (_key(values),)
        ).fetchone():
            raise EripError(
                "У этой карты уже есть условия ЕРИП. Откройте её и предложите поправку."
            )

    def publish(self, connection: sqlite3.Connection, payload: dict[str, Any]) -> int:
        self.check(connection, payload)
        values = {
            key: value for key, value in payload.items() if key not in {"rule_id", "revision"}
        }
        if "rule_id" in payload:
            connection.execute(
                "UPDATE erip_rules SET data=?, revision=revision+1 WHERE id=?",
                (_encoded(values), payload["rule_id"]),
            )
            return payload["rule_id"]
        return connection.execute(
            "INSERT INTO erip_rules(card_key,data) VALUES(?,?)", (_key(values), _encoded(values))
        ).lastrowid
