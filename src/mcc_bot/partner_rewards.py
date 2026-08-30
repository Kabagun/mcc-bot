"""Persistent store-specific card rewards and partner-aware calculation.

Partner data is deliberately separate from the MCC catalog.  A raw MCC lookup
therefore remains stable, while a store lookup may replace or add a reward for
one card.  Offers and exclusions share the stores SQLite database so community
publication can update both models in one transaction.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import Decimal

from .catalog import (
    CardCatalog,
    CardMatch,
    RewardComponent,
    RewardProgram,
)
from .stores import StoreRepository

CHANNELS = frozenset({"offline", "online", "any"})
MODES = frozenset({"additional", "total"})
REWARD_KINDS = frozenset({"cash", "points"})
MCC_RE = re.compile(r"[0-9]{4}")


class PartnerRewardError(ValueError):
    """Raised when partner data is invalid or no longer available."""


@dataclass(frozen=True, slots=True)
class PartnerTierInput:
    """One amount/date tier in a partner offer."""

    value: Decimal
    min_purchase: Decimal | None = None
    max_purchase: Decimal | None = None
    per_transaction_cap: Decimal | None = None
    starts_on: date | None = None
    ends_on: date | None = None


@dataclass(frozen=True, slots=True)
class PartnerExclusionInput:
    """A partner exclusion with an optional explicit base-program suppression."""

    brand_id: int | None
    card_id: str
    reward_kind: str
    channel: str = "any"
    mcc: str | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    suppress_base: bool = False
    reason: str = ""
    source_url: str = ""


@dataclass(frozen=True, slots=True)
class PartnerOfferInput:
    """Validated input used by community publication and static seeds.

    ``source_url`` may be empty because community evidence can instead be a
    screenshot.  The community layer owns the URL-or-media requirement.
    """

    brand_id: int
    card_id: str
    channel: str
    mode: str
    reward_kind: str
    tiers: tuple[PartnerTierInput, ...]
    starts_on: date | None = None
    ends_on: date | None = None
    conditions: str = ""
    source_url: str = ""
    exclusions: tuple[PartnerExclusionInput, ...] = ()


@dataclass(frozen=True, slots=True)
class PartnerTier:
    """Persisted offer tier."""

    id: int
    value: Decimal
    min_purchase: Decimal | None
    max_purchase: Decimal | None
    per_transaction_cap: Decimal | None
    starts_on: date | None
    ends_on: date | None


@dataclass(frozen=True, slots=True)
class PartnerOffer:
    """Persisted store/card partner offer."""

    id: int
    source_key: str | None
    brand_id: int
    card_id: str
    channel: str
    mode: str
    reward_kind: str
    tiers: tuple[PartnerTier, ...]
    starts_on: date | None
    ends_on: date | None
    conditions: str
    source_url: str
    archived: bool

    @property
    def maximum_value(self) -> Decimal:
        """Return the highest advertised tier for no-amount display/ranking."""

        return max(tier.value for tier in self.tiers)


@dataclass(frozen=True, slots=True)
class PartnerExclusion:
    """Persisted store/card exclusion."""

    id: int
    source_key: str | None
    brand_id: int | None
    card_id: str
    reward_kind: str
    channel: str
    mcc: str | None
    starts_on: date | None
    ends_on: date | None
    suppress_base: bool
    reason: str
    source_url: str
    archived: bool


@dataclass(frozen=True, slots=True)
class PartnerResolution:
    """One effective partner result for a store/card/date/amount."""

    offer: PartnerOffer | None
    tier: PartnerTier | None
    exclusion: PartnerExclusion | None

    @property
    def excluded(self) -> bool:
        """Return whether partner reward falls back to the ordinary component."""

        return self.exclusion is not None

    @property
    def value(self) -> Decimal | None:
        """Return the resolved tier value, if an offer applies."""

        return self.tier.value if self.tier else None


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _date_value(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _decimal_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _decimal_value(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _validate_decimal(value: Decimal | None, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise PartnerRewardError(f"{field} должен быть неотрицательным Decimal")


def _validate_dates(starts_on: date | None, ends_on: date | None, field: str) -> None:
    if starts_on is not None and not isinstance(starts_on, date):
        raise PartnerRewardError(f"{field}.starts_on должен быть датой")
    if ends_on is not None and not isinstance(ends_on, date):
        raise PartnerRewardError(f"{field}.ends_on должен быть датой")
    if starts_on and ends_on and starts_on > ends_on:
        raise PartnerRewardError(f"{field}: дата начала позже даты окончания")


def _validate_offer(payload: PartnerOfferInput) -> None:
    if isinstance(payload.brand_id, bool) or payload.brand_id < 1:
        raise PartnerRewardError("brand_id должен быть положительным целым числом")
    if not payload.card_id.strip():
        raise PartnerRewardError("card_id обязателен")
    if payload.channel not in CHANNELS:
        raise PartnerRewardError("channel должен быть offline, online или any")
    if payload.mode not in MODES:
        raise PartnerRewardError("mode должен быть additional или total")
    if payload.reward_kind not in REWARD_KINDS:
        raise PartnerRewardError("reward_kind должен быть cash или points")
    if not payload.tiers:
        raise PartnerRewardError("Нужен хотя бы один тариф партнёра")
    _validate_dates(payload.starts_on, payload.ends_on, "offer")
    for index, tier in enumerate(payload.tiers):
        _validate_decimal(tier.value, f"tiers[{index}].value")
        _validate_decimal(tier.min_purchase, f"tiers[{index}].min_purchase")
        _validate_decimal(tier.max_purchase, f"tiers[{index}].max_purchase")
        _validate_decimal(tier.per_transaction_cap, f"tiers[{index}].per_transaction_cap")
        _validate_dates(tier.starts_on, tier.ends_on, f"tiers[{index}]")
        if (
            tier.min_purchase is not None
            and tier.max_purchase is not None
            and tier.min_purchase > tier.max_purchase
        ):
            raise PartnerRewardError(f"tiers[{index}]: неверный диапазон суммы")


def _validate_exclusion(payload: PartnerExclusionInput) -> None:
    if payload.brand_id is not None and (
        isinstance(payload.brand_id, bool) or payload.brand_id < 1
    ):
        raise PartnerRewardError("brand_id должен быть положительным целым числом или None")
    if not payload.card_id.strip() or payload.reward_kind not in REWARD_KINDS:
        raise PartnerRewardError("Для исключения нужны card_id и reward_kind")
    if payload.channel not in CHANNELS:
        raise PartnerRewardError("channel должен быть offline, online или any")
    if payload.mcc is not None and MCC_RE.fullmatch(payload.mcc) is None:
        raise PartnerRewardError("MCC исключения должен состоять из четырёх цифр")
    if not isinstance(payload.suppress_base, bool):
        raise PartnerRewardError("suppress_base должен быть логическим значением")
    _validate_dates(payload.starts_on, payload.ends_on, "exclusion")


class PartnerRepository:
    """CRUD, seed, and resolution API for the stores SQLite database."""

    def __init__(self, stores: StoreRepository) -> None:
        self.stores = stores

    @contextmanager
    def _write(self, connection: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
            return
        with self.stores.transaction() as owned:
            yield owned

    @contextmanager
    def _read(self, connection: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
            return
        with self.stores.connection() as owned:
            yield owned

    def initialize(self) -> None:
        """Create the additive partner schema without inserting seed data."""

        with self.stores.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS partner_offers (
                    id INTEGER PRIMARY KEY,
                    source_key TEXT UNIQUE,
                    brand_id INTEGER NOT NULL REFERENCES store_brands(id),
                    card_id TEXT NOT NULL,
                    channel TEXT NOT NULL CHECK(channel IN ('offline','online','any')),
                    mode TEXT NOT NULL CHECK(mode IN ('additional','total')),
                    reward_kind TEXT NOT NULL CHECK(reward_kind IN ('cash','points')),
                    starts_on TEXT,
                    ends_on TEXT,
                    conditions TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER NOT NULL,
                    updated_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS partner_offers_lookup
                    ON partner_offers(brand_id,card_id,channel,archived);
                CREATE TABLE IF NOT EXISTS partner_offer_tiers (
                    id INTEGER PRIMARY KEY,
                    offer_id INTEGER NOT NULL REFERENCES partner_offers(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL,
                    value TEXT NOT NULL,
                    min_purchase TEXT,
                    max_purchase TEXT,
                    per_transaction_cap TEXT,
                    starts_on TEXT,
                    ends_on TEXT,
                    UNIQUE(offer_id,position)
                );
                CREATE TABLE IF NOT EXISTS partner_exclusions (
                    id INTEGER PRIMARY KEY,
                    source_key TEXT UNIQUE,
                    brand_id INTEGER REFERENCES store_brands(id),
                    card_id TEXT NOT NULL,
                    reward_kind TEXT NOT NULL CHECK(reward_kind IN ('cash','points')),
                    channel TEXT NOT NULL CHECK(channel IN ('offline','online','any')),
                    mcc TEXT,
                    starts_on TEXT,
                    ends_on TEXT,
                    suppress_base INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_by INTEGER NOT NULL,
                    updated_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS partner_exclusions_lookup
                    ON partner_exclusions(brand_id,card_id,channel,archived);
                CREATE TABLE IF NOT EXISTS partner_audit (
                    id INTEGER PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS partner_seed_brands (
                    source_key TEXT PRIMARY KEY,
                    brand_id INTEGER NOT NULL REFERENCES store_brands(id)
                );
                """
            )
            exclusion_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(partner_exclusions)")
            }
            if "suppress_base" not in exclusion_columns:
                connection.execute(
                    "ALTER TABLE partner_exclusions "
                    "ADD COLUMN suppress_base INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _actor(actor_id: int) -> None:
        if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id < 1:
            raise PartnerRewardError("actor_id должен быть положительным целым числом")

    def _offer_from_row(self, connection: sqlite3.Connection, row) -> PartnerOffer:
        tiers = tuple(
            PartnerTier(
                id=item["id"],
                value=Decimal(item["value"]),
                min_purchase=_decimal_value(item["min_purchase"]),
                max_purchase=_decimal_value(item["max_purchase"]),
                per_transaction_cap=_decimal_value(item["per_transaction_cap"]),
                starts_on=_date_value(item["starts_on"]),
                ends_on=_date_value(item["ends_on"]),
            )
            for item in connection.execute(
                "SELECT * FROM partner_offer_tiers WHERE offer_id=? ORDER BY position,id",
                (row["id"],),
            )
        )
        return PartnerOffer(
            id=row["id"],
            source_key=row["source_key"],
            brand_id=row["brand_id"],
            card_id=row["card_id"],
            channel=row["channel"],
            mode=row["mode"],
            reward_kind=row["reward_kind"],
            tiers=tiers,
            starts_on=_date_value(row["starts_on"]),
            ends_on=_date_value(row["ends_on"]),
            conditions=row["conditions"],
            source_url=row["source_url"],
            archived=bool(row["archived"]),
        )

    @staticmethod
    def _exclusion_from_row(row) -> PartnerExclusion:
        return PartnerExclusion(
            id=row["id"],
            source_key=row["source_key"],
            brand_id=row["brand_id"],
            card_id=row["card_id"],
            reward_kind=row["reward_kind"],
            channel=row["channel"],
            mcc=row["mcc"],
            starts_on=_date_value(row["starts_on"]),
            ends_on=_date_value(row["ends_on"]),
            suppress_base=bool(row["suppress_base"]),
            reason=row["reason"],
            source_url=row["source_url"],
            archived=bool(row["archived"]),
        )

    def _insert_tiers(
        self, connection: sqlite3.Connection, offer_id: int, tiers: tuple[PartnerTierInput, ...]
    ) -> None:
        connection.executemany(
            """INSERT INTO partner_offer_tiers
            (offer_id,position,value,min_purchase,max_purchase,per_transaction_cap,starts_on,ends_on)
            VALUES(?,?,?,?,?,?,?,?)""",
            [
                (
                    offer_id,
                    position,
                    _decimal_text(tier.value),
                    _decimal_text(tier.min_purchase),
                    _decimal_text(tier.max_purchase),
                    _decimal_text(tier.per_transaction_cap),
                    _date_text(tier.starts_on),
                    _date_text(tier.ends_on),
                )
                for position, tier in enumerate(tiers)
            ],
        )

    @staticmethod
    def _audit(
        connection, entity_type: str, entity_id: int, action: str, actor_id: int, snapshot
    ) -> None:
        connection.execute(
            """INSERT INTO partner_audit(entity_type,entity_id,action,actor_id,snapshot_json)
            VALUES(?,?,?,?,?)""",
            (
                entity_type,
                entity_id,
                action,
                actor_id,
                json.dumps(snapshot, ensure_ascii=False, default=str),
            ),
        )

    def create_offer(
        self,
        payload: PartnerOfferInput,
        *,
        actor_id: int,
        connection: sqlite3.Connection | None = None,
        source_key: str | None = None,
    ) -> PartnerOffer:
        """Create an offer, optionally inside the caller's transaction."""

        _validate_offer(payload)
        self._actor(actor_id)
        with self._write(connection) as current:
            brand = current.execute(
                "SELECT 1 FROM store_brands WHERE id=?", (payload.brand_id,)
            ).fetchone()
            if brand is None:
                raise PartnerRewardError("Магазин для партнёрского предложения не найден")
            try:
                cursor = current.execute(
                    """INSERT INTO partner_offers
                    (source_key,brand_id,card_id,channel,mode,reward_kind,starts_on,ends_on,
                     conditions,source_url,created_by,updated_by)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        source_key,
                        payload.brand_id,
                        payload.card_id.strip(),
                        payload.channel,
                        payload.mode,
                        payload.reward_kind,
                        _date_text(payload.starts_on),
                        _date_text(payload.ends_on),
                        payload.conditions.strip(),
                        payload.source_url.strip(),
                        actor_id,
                        actor_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PartnerRewardError("Партнёрское предложение уже существует") from exc
            offer_id = int(cursor.lastrowid)
            self._insert_tiers(current, offer_id, payload.tiers)
            row = current.execute("SELECT * FROM partner_offers WHERE id=?", (offer_id,)).fetchone()
            offer = self._offer_from_row(current, row)
            self._audit(current, "offer", offer_id, "create", actor_id, asdict(offer))
            for exclusion in payload.exclusions:
                self.create_exclusion(exclusion, actor_id=actor_id, connection=current)
            return offer

    def update_offer(
        self,
        offer_id: int,
        payload: PartnerOfferInput,
        *,
        actor_id: int,
        connection: sqlite3.Connection | None = None,
    ) -> PartnerOffer:
        """Replace editable offer fields and tiers while preserving identity."""

        _validate_offer(payload)
        self._actor(actor_id)
        with self._write(connection) as current:
            before = self.get_offer(offer_id, include_archived=True, connection=current)
            if before is None:
                raise PartnerRewardError("Партнёрское предложение не найдено")
            current.execute(
                """UPDATE partner_offers SET brand_id=?,card_id=?,channel=?,mode=?,reward_kind=?,
                starts_on=?,ends_on=?,conditions=?,source_url=?,updated_by=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=?""",
                (
                    payload.brand_id,
                    payload.card_id.strip(),
                    payload.channel,
                    payload.mode,
                    payload.reward_kind,
                    _date_text(payload.starts_on),
                    _date_text(payload.ends_on),
                    payload.conditions.strip(),
                    payload.source_url.strip(),
                    actor_id,
                    offer_id,
                ),
            )
            current.execute("DELETE FROM partner_offer_tiers WHERE offer_id=?", (offer_id,))
            self._insert_tiers(current, offer_id, payload.tiers)
            result = self.get_offer(offer_id, include_archived=True, connection=current)
            assert result is not None
            self._audit(
                current,
                "offer",
                offer_id,
                "update",
                actor_id,
                {"before": asdict(before), "after": asdict(result)},
            )
            return result

    def delete_offer(
        self,
        offer_id: int,
        *,
        actor_id: int,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        """Soft-delete an offer; return false when already absent/archived."""

        self._actor(actor_id)
        with self._write(connection) as current:
            offer = self.get_offer(offer_id, include_archived=True, connection=current)
            if offer is None or offer.archived:
                return False
            current.execute(
                """UPDATE partner_offers SET archived=1,updated_by=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (actor_id, offer_id),
            )
            self._audit(current, "offer", offer_id, "delete", actor_id, asdict(offer))
            return True

    def restore_offer(
        self,
        offer_id: int,
        *,
        actor_id: int,
        connection: sqlite3.Connection | None = None,
    ) -> PartnerOffer:
        """Restore a soft-deleted offer."""

        self._actor(actor_id)
        with self._write(connection) as current:
            offer = self.get_offer(offer_id, include_archived=True, connection=current)
            if offer is None:
                raise PartnerRewardError("Партнёрское предложение не найдено")
            current.execute(
                """UPDATE partner_offers SET archived=0,updated_by=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (actor_id, offer_id),
            )
            result = self.get_offer(offer_id, include_archived=True, connection=current)
            assert result is not None
            self._audit(current, "offer", offer_id, "restore", actor_id, asdict(result))
            return result

    def get_offer(
        self,
        offer_id: int,
        *,
        include_archived: bool = False,
        connection: sqlite3.Connection | None = None,
    ) -> PartnerOffer | None:
        """Read one offer by durable ID."""

        with self._read(connection) as current:
            suffix = "" if include_archived else " AND archived=0"
            row = current.execute(
                f"SELECT * FROM partner_offers WHERE id=?{suffix}", (offer_id,)
            ).fetchone()
            return self._offer_from_row(current, row) if row else None

    def list_offers(
        self,
        brand_id: int,
        *,
        include_archived: bool = False,
        card_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[PartnerOffer, ...]:
        """List stable offers for a store, optionally narrowed to one card."""

        with self._read(connection) as current:
            clauses, values = ["brand_id=?"], [brand_id]
            if not include_archived:
                clauses.append("archived=0")
            if card_id is not None:
                clauses.append("card_id=?")
                values.append(card_id)
            rows = current.execute(
                "SELECT * FROM partner_offers WHERE "
                + " AND ".join(clauses)
                + " ORDER BY card_id,id",
                values,
            ).fetchall()
            return tuple(self._offer_from_row(current, row) for row in rows)

    def create_exclusion(
        self,
        payload: PartnerExclusionInput,
        *,
        actor_id: int,
        connection: sqlite3.Connection | None = None,
        source_key: str | None = None,
    ) -> PartnerExclusion:
        """Create one store/card exclusion."""

        _validate_exclusion(payload)
        self._actor(actor_id)
        with self._write(connection) as current:
            try:
                cursor = current.execute(
                    """INSERT INTO partner_exclusions
                    (source_key,brand_id,card_id,reward_kind,channel,mcc,starts_on,ends_on,
                     suppress_base,reason,source_url,created_by,updated_by)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        source_key,
                        payload.brand_id,
                        payload.card_id.strip(),
                        payload.reward_kind,
                        payload.channel,
                        payload.mcc,
                        _date_text(payload.starts_on),
                        _date_text(payload.ends_on),
                        int(payload.suppress_base),
                        payload.reason.strip(),
                        payload.source_url.strip(),
                        actor_id,
                        actor_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PartnerRewardError("Партнёрское исключение уже существует") from exc
            row = current.execute(
                "SELECT * FROM partner_exclusions WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            result = self._exclusion_from_row(row)
            self._audit(current, "exclusion", result.id, "create", actor_id, asdict(result))
            return result

    def list_exclusions(
        self,
        brand_id: int | None,
        *,
        card_id: str | None = None,
        include_archived: bool = False,
        include_global: bool = True,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[PartnerExclusion, ...]:
        """List brand exclusions plus global MCC rules for a store."""

        with self._read(connection) as current:
            if brand_id is None:
                clauses, values = ["brand_id IS NULL"], []
            elif include_global:
                clauses, values = ["(brand_id=? OR brand_id IS NULL)"], [brand_id]
            else:
                clauses, values = ["brand_id=?"], [brand_id]
            if card_id is not None:
                clauses.append("card_id=?")
                values.append(card_id)
            if not include_archived:
                clauses.append("archived=0")
            rows = current.execute(
                "SELECT * FROM partner_exclusions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY card_id,id",
                values,
            ).fetchall()
            return tuple(self._exclusion_from_row(row) for row in rows)

    def update_exclusion(
        self,
        exclusion_id: int,
        payload: PartnerExclusionInput,
        *,
        actor_id: int,
        connection: sqlite3.Connection | None = None,
    ) -> PartnerExclusion:
        """Replace editable exclusion fields while preserving its durable ID."""

        _validate_exclusion(payload)
        self._actor(actor_id)
        with self._write(connection) as current:
            row = current.execute(
                "SELECT * FROM partner_exclusions WHERE id=?", (exclusion_id,)
            ).fetchone()
            if row is None:
                raise PartnerRewardError("Партнёрское исключение не найдено")
            before = self._exclusion_from_row(row)
            current.execute(
                """UPDATE partner_exclusions SET brand_id=?,card_id=?,reward_kind=?,channel=?,
                mcc=?,starts_on=?,ends_on=?,suppress_base=?,reason=?,source_url=?,updated_by=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    payload.brand_id,
                    payload.card_id.strip(),
                    payload.reward_kind,
                    payload.channel,
                    payload.mcc,
                    _date_text(payload.starts_on),
                    _date_text(payload.ends_on),
                    int(payload.suppress_base),
                    payload.reason.strip(),
                    payload.source_url.strip(),
                    actor_id,
                    exclusion_id,
                ),
            )
            updated = current.execute(
                "SELECT * FROM partner_exclusions WHERE id=?", (exclusion_id,)
            ).fetchone()
            result = self._exclusion_from_row(updated)
            self._audit(
                current,
                "exclusion",
                exclusion_id,
                "update",
                actor_id,
                {"before": asdict(before), "after": asdict(result)},
            )
            return result

    def delete_exclusion(
        self,
        exclusion_id: int,
        *,
        actor_id: int,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        """Soft-delete an exclusion; return false when already absent/archived."""

        self._actor(actor_id)
        with self._write(connection) as current:
            row = current.execute(
                "SELECT * FROM partner_exclusions WHERE id=?", (exclusion_id,)
            ).fetchone()
            if row is None or bool(row["archived"]):
                return False
            before = self._exclusion_from_row(row)
            current.execute(
                """UPDATE partner_exclusions SET archived=1,updated_by=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (actor_id, exclusion_id),
            )
            self._audit(current, "exclusion", exclusion_id, "delete", actor_id, asdict(before))
            return True

    @staticmethod
    def _active(starts_on: date | None, ends_on: date | None, on_date: date) -> bool:
        return (starts_on is None or starts_on <= on_date) and (
            ends_on is None or on_date <= ends_on
        )

    @staticmethod
    def _tier_applies(tier: PartnerTier, amount: Decimal | None, on_date: date) -> bool:
        if not PartnerRepository._active(tier.starts_on, tier.ends_on, on_date):
            return False
        if amount is None:
            return True
        return (tier.min_purchase is None or tier.min_purchase <= amount) and (
            tier.max_purchase is None or amount <= tier.max_purchase
        )

    def resolve_all(
        self,
        brand_id: int,
        card_id: str,
        channel: str,
        mcc: str,
        *,
        amount: Decimal | None = None,
        on_date: date | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[PartnerResolution, ...]:
        """Resolve effective exclusions/offers independently by reward kind."""

        if channel not in {"offline", "online"} or MCC_RE.fullmatch(mcc) is None:
            raise PartnerRewardError("Для расчёта нужны канал offline/online и четырёхзначный MCC")
        _validate_decimal(amount, "amount")
        current_date = on_date or date.today()
        exclusions = self.list_exclusions(brand_id, card_id=card_id, connection=connection)
        effective_exclusions = [
            item
            for item in exclusions
            if item.channel in {"any", channel}
            and item.mcc in {None, mcc}
            and self._active(item.starts_on, item.ends_on, current_date)
        ]
        effective_exclusions.sort(
            key=lambda item: (item.mcc is None, item.channel == "any", item.id)
        )
        exclusions_by_kind: dict[str, PartnerExclusion] = {}
        for exclusion in effective_exclusions:
            selected = exclusions_by_kind.get(exclusion.reward_kind)
            if selected is None:
                exclusions_by_kind[exclusion.reward_kind] = exclusion
            elif exclusion.suppress_base and not selected.suppress_base:
                exclusions_by_kind[exclusion.reward_kind] = replace(selected, suppress_base=True)
        candidates: list[tuple[Decimal, PartnerOffer, PartnerTier]] = []
        for offer in self.list_offers(brand_id, card_id=card_id, connection=connection):
            if offer.channel not in {"any", channel} or not self._active(
                offer.starts_on, offer.ends_on, current_date
            ):
                continue
            if offer.reward_kind in exclusions_by_kind:
                continue
            tiers = [tier for tier in offer.tiers if self._tier_applies(tier, amount, current_date)]
            if tiers:
                tier = max(tiers, key=lambda item: (item.value, -item.id))
                candidates.append((tier.value, offer, tier))
        result = [
            PartnerResolution(None, None, exclusion) for exclusion in exclusions_by_kind.values()
        ]
        totals = [candidate for candidate in candidates if candidate[1].mode == "total"]
        if totals:
            _value, offer, tier = max(totals, key=lambda item: (item[0], -item[1].id))
            result.append(PartnerResolution(offer, tier, None))
        else:
            additions: dict[str, list[tuple[Decimal, PartnerOffer, PartnerTier]]] = {}
            for candidate in candidates:
                additions.setdefault(candidate[1].reward_kind, []).append(candidate)
            for values in additions.values():
                _value, offer, tier = max(values, key=lambda item: (item[0], -item[1].id))
                result.append(PartnerResolution(offer, tier, None))
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.exclusion.reward_kind if item.exclusion else item.offer.reward_kind,
                    item.exclusion.id if item.exclusion else item.offer.id,
                ),
            )
        )

    def resolve(
        self,
        brand_id: int,
        card_id: str,
        channel: str,
        mcc: str,
        *,
        amount: Decimal | None = None,
        on_date: date | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> PartnerResolution | None:
        """Return the highest single resolution for simple adapter consumers."""

        resolutions = self.resolve_all(
            brand_id,
            card_id,
            channel,
            mcc,
            amount=amount,
            on_date=on_date,
            connection=connection,
        )
        if not resolutions:
            return None
        return max(
            resolutions,
            key=lambda item: (
                item.value if item.value is not None else Decimal("-1"),
                item.exclusion is None,
            ),
        )

    def list_active_offers(
        self,
        brand_id: int,
        *,
        channel: str | None = None,
        amount: Decimal | None = None,
        on_date: date | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[tuple[PartnerOffer, PartnerTier], ...]:
        """List active offers with the best applicable tier for store presentation."""

        if channel is not None and channel not in {"offline", "online"}:
            raise PartnerRewardError("channel должен быть offline или online")
        _validate_decimal(amount, "amount")
        current_date = on_date or date.today()
        result = []
        for offer in self.list_offers(brand_id, connection=connection):
            if channel is not None and offer.channel not in {"any", channel}:
                continue
            if not self._active(offer.starts_on, offer.ends_on, current_date):
                continue
            tiers = [tier for tier in offer.tiers if self._tier_applies(tier, amount, current_date)]
            if tiers:
                result.append((offer, max(tiers, key=lambda item: (item.value, -item.id))))
        return tuple(sorted(result, key=lambda item: (item[0].card_id, item[0].id)))

    def ensure_seed_offer(
        self,
        source_key: str,
        payload: PartnerOfferInput,
        *,
        actor_id: int,
        connection: sqlite3.Connection,
    ) -> tuple[PartnerOffer, bool]:
        """Insert a static offer once; never overwrite later human changes."""

        row = connection.execute(
            "SELECT id FROM partner_offers WHERE source_key=?", (source_key,)
        ).fetchone()
        if row:
            offer = self.get_offer(row["id"], include_archived=True, connection=connection)
            assert offer is not None
            return offer, False
        offer = self.create_offer(
            payload, actor_id=actor_id, connection=connection, source_key=source_key
        )
        return offer, True

    def ensure_seed_exclusion(
        self,
        source_key: str,
        payload: PartnerExclusionInput,
        *,
        actor_id: int,
        connection: sqlite3.Connection,
    ) -> tuple[PartnerExclusion, bool]:
        """Insert a static exclusion once; never overwrite later human changes."""

        row = connection.execute(
            "SELECT * FROM partner_exclusions WHERE source_key=?", (source_key,)
        ).fetchone()
        if row:
            return self._exclusion_from_row(row), False
        exclusion = self.create_exclusion(
            payload, actor_id=actor_id, connection=connection, source_key=source_key
        )
        return exclusion, True


def _partner_program(offer: PartnerOffer) -> RewardProgram:
    return RewardProgram(
        id=f"partner:{offer.id}",
        kind=offer.reward_kind,
        tax_exempt=offer.reward_kind == "points",
        offers=(),
        default_value=None,
        excluded_mccs=frozenset(),
        minimum_payment=None,
        maximum_reward=None,
        monthly_maximum_not_defined=False,
        maximum_reward_alternatives=(),
        domestic_country=None,
        foreign_value=None,
    )


def _display_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered.replace(".", ",")


def format_partner_offer_context(offer: PartnerOffer, tier: PartnerTier) -> str:
    """Render only purchase constraints that matter for the selected store MCC."""

    details = []
    condition = offer.conditions.strip().rstrip(".")
    normalized_condition = condition.casefold().translate({0x451: 0x435})
    redundant_conditions = {
        "только при онлайн-оплате",
        "только при офлайн-оплате",
        "только у партнера",  # noqa: RUF001
        "общий максимальный манибэк у партнера по карте 1-2-3",  # noqa: RUF001
    }
    if condition and normalized_condition not in redundant_conditions:
        details.append(condition)
    if tier.min_purchase is not None and tier.max_purchase is not None:
        details.append(
            f"сумма {_display_decimal(tier.min_purchase)}–{_display_decimal(tier.max_purchase)} BYN"  # noqa: RUF001
        )
    elif tier.min_purchase is not None:
        details.append(f"сумма от {_display_decimal(tier.min_purchase)} BYN")
    elif tier.max_purchase is not None:
        details.append(f"сумма до {_display_decimal(tier.max_purchase)} BYN")
    if tier.per_transaction_cap is not None:
        unit = "баллов" if offer.reward_kind == "points" else "BYN"
        details.append(f"не больше {_display_decimal(tier.per_transaction_cap)} {unit} за операцию")
    ends_on = tier.ends_on or offer.ends_on
    if ends_on:
        details.append(f"по {ends_on.strftime('%d.%m.%Y')}")
    return " · ".join(details)


def resolve_store_matches(
    catalog: CardCatalog,
    partners: PartnerRepository,
    brand_id: int,
    channel: str,
    mcc: str,
    *,
    amount: Decimal | None = None,
    on_date: date | None = None,
) -> tuple[CardMatch, ...]:
    """Compose ordinary MCC rewards with store-specific partner rewards.

    Exclusions remove only their named reward kind, so (for example) excluded
    partner points fall back to ordinary money cashback. Total offers replace
    the complete ordinary result; additional offers append a separately
    displayed component.
    """

    base = {match.card.id: match for match in catalog.lookup(mcc)}
    cards = {card.id: card for card in catalog.cards}
    relevant = {offer.card_id for offer in partners.list_offers(brand_id)} | {
        exclusion.card_id for exclusion in partners.list_exclusions(brand_id)
    }
    results: list[CardMatch] = []
    for card_id in set(base) | relevant:
        match = base.get(card_id)
        card = match.card if match else cards.get(card_id)
        if card is None:
            continue
        components = list(match.components if match else ())
        resolutions = partners.resolve_all(
            brand_id, card_id, channel, mcc, amount=amount, on_date=on_date
        )
        context_lines = []
        for resolution in resolutions:
            if resolution.excluded:
                assert resolution.exclusion is not None
                exclusion = resolution.exclusion
                if exclusion.suppress_base:
                    components = [
                        component
                        for component in components
                        if component.kind != exclusion.reward_kind
                    ]
                components = [
                    replace(
                        component,
                        display_kind=(" деньгами" if component.kind == "cash" else " баллами"),
                    )
                    for component in components
                ]
                reason = exclusion.reason or "магазин исключён из бонусной программы"
                if exclusion.card_id == "vitamin_d" and exclusion.reward_kind == "points":
                    label = "Баллы Плюшек"
                elif exclusion.reward_kind == "points":
                    label = "Партнёрские баллы"
                else:
                    label = "Партнёрский манибэк"
                verb = "не начисляются" if exclusion.reward_kind == "points" else "не начисляется"
                context_lines.append(f"{label} {verb}: {reason}")
                continue
            assert resolution.offer is not None and resolution.tier is not None
            offer, tier = resolution.offer, resolution.tier
            if offer.mode == "total":
                # An advertised total is the complete partner result, even when
                # the ordinary catalog component uses another unit (for example
                # ordinary cash versus a final Cactus points rate).
                components = []
            else:
                components = [
                    replace(component, display_kind="")
                    if component.kind == offer.reward_kind
                    else component
                    for component in components
                ]
            component = RewardComponent(
                program_id=f"partner:{offer.id}",
                kind=offer.reward_kind,
                gross_value=tier.value,
                tax_exempt=offer.reward_kind == "points",
                display_kind=" баллами" if offer.reward_kind == "points" else " деньгами",
            )
            components.append(component)
            card = replace(card, reward_programs=(*card.reward_programs, _partner_program(offer)))
            offer_context = format_partner_offer_context(offer, tier)
            if offer_context:
                context_lines.append(offer_context)
        if components:
            results.append(
                CardMatch(
                    card=card,
                    mcc=mcc,
                    components=tuple(components),
                    context_lines=tuple(context_lines),
                )
            )
    results.sort(key=lambda item: (-item.gross_value, item.card.name.casefold(), item.card.id))
    return tuple(results)
