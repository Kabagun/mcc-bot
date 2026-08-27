"""Canonical merchants, independent MCC evidence and reversible audited edits.

Names are search keys, never identities. Import identities are exact source IDs;
offline networks and online applications remain separate merchants. Every write
can share the moderation transaction through the ``connection`` argument.
"""

# Merchant names and user-facing messages intentionally use Cyrillic.
# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .catalog import normalize_mcc


class StoreError(ValueError):
    """Invalid merchant change or missing entity."""


class StaleChangeError(StoreError):
    """A reversal would overwrite a later independent change."""


@dataclass(frozen=True, slots=True)
class Merchant:
    """One canonical merchant in a single payment channel."""

    id: int
    name: str
    channel: str
    aliases: tuple[str, ...]
    archived: bool
    revision: int


@dataclass(frozen=True, slots=True)
class MccFact:
    """A unique MCC association, supported by independent evidence records."""

    merchant_id: int
    mcc: str
    archived: bool
    revision: int
    evidence_count: int


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Deterministic matches and separately labelled fuzzy suggestions."""

    matches: tuple[Merchant, ...]
    suggestions: tuple[Merchant, ...] = ()
    total: int = 0


@dataclass(frozen=True, slots=True)
class ChangeResult:
    """Durable change identity returned to moderation."""

    audit_id: int
    merchant_id: int


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """Privacy-safe historical change summary, without contributor evidence."""

    id: int
    kind: str
    merchant_id: int
    actor_id: int
    created_at: str
    reverted_by: int | None
    details: tuple[str, ...]


_CYRILLIC = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюяіў",
        (
            "a",
            "b",
            "v",
            "g",
            "d",
            "e",
            "e",
            "zh",
            "z",
            "i",
            "i",
            "k",
            "l",
            "m",
            "n",
            "o",
            "p",
            "r",
            "s",
            "t",
            "u",
            "f",
            "h",
            "ts",
            "ch",
            "sh",
            "shch",
            "",
            "y",
            "",
            "e",
            "yu",
            "ya",
            "i",
            "u",
        ),
        strict=True,
    )
)


def normalize_store_name(value: str) -> str:
    """Normalize case, transliteration, spaces and punctuation for search only."""

    folded = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(_CYRILLIC.get(char, char) for char in folded if not unicodedata.combining(char))
    text = "".join(char for char in text if char.isalnum())
    # Both spellings are widely used for the same transliterated prefix.
    return text.replace("euro", "evro")


def _script(value: str) -> str | None:
    folded = unicodedata.normalize("NFKD", value.casefold())
    latin = any("a" <= char <= "z" for char in folded)
    cyrillic = any("\u0400" <= char <= "\u052f" for char in folded)
    if latin == cyrillic:
        return None
    return "latin" if latin else "cyrillic"


def _relaxed_search_key(value: str) -> str:
    """Return a conservative cross-script pronunciation key for search only."""

    # English ``ee`` is commonly written as Cyrillic ``и`` in brand names:
    # Green -> грин. Keep this separate from identity/duplicate normalization.
    return normalize_store_name(value).replace("ee", "i")


def _relaxed_cross_script_match(query: str, candidate: str, needle: str) -> bool:
    query_script, candidate_script = _script(query), _script(candidate)
    return (
        len(needle) >= 4
        and query_script is not None
        and candidate_script is not None
        and query_script != candidate_script
        and _relaxed_search_key(query) == _relaxed_search_key(candidate)
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 180:
        raise StoreError("Название должно содержать от 1 до 180 символов")
    value = value.strip()
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise StoreError("Название содержит недопустимые символы")
    return value


def _channel(value: Any) -> str:
    if value not in {"offline", "online"}:
        raise StoreError("Выберите обычный магазин или онлайн/приложение")
    return value


def _aliases(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 30:
        raise StoreError("Допустимо до 30 вариантов названия")
    return list(dict.fromkeys(_name(item) for item in value))


class _Changes:
    """Record only touched rows; audit snapshots never contain media identifiers."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.before: dict[tuple[str, int], dict | None] = {}

    def touch(self, table: str, row_id: int, *, new: bool = False) -> None:
        key = table, row_id
        if key not in self.before:
            row = (
                None
                if new
                else self.connection.execute(
                    f"SELECT * FROM {table} WHERE id=?", (row_id,)
                ).fetchone()
            )
            self.before[key] = dict(row) if row is not None else None

    def finish(self) -> list[dict]:
        result = []
        for (table, row_id), before in self.before.items():
            row = self.connection.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
            after = dict(row) if row is not None else None
            if before != after or table == "store_facts":
                result.append({"table": table, "id": row_id, "before": before, "after": after})
        return result


class StoreRepository:
    """SQLite store repository; writers serialize with ``BEGIN IMMEDIATE``."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._memory = str(path) == ":memory:"
        self._uri = f"file:stores-{uuid.uuid4().hex}?mode=memory&cache=shared"
        self._anchor: sqlite3.Connection | None = None

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._uri if self._memory else self.path,
            uri=self._memory,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def connection(self):
        """Yield a short-lived connection; callers own explicit transactions."""

        connection = self._open()
        try:
            yield connection
        finally:
            connection.close()

    connect = connection

    @contextmanager
    def transaction(self):
        """Atomically commit one write or roll back all its effects on error."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def initialize(self) -> None:
        """Create private durable schema, without changing existing facts."""

        if self._memory:
            if self._anchor is None:
                self._anchor = self._open()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS store_merchants (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, channel TEXT NOT NULL,
                    aliases_json TEXT NOT NULL DEFAULT '[]', archived INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1, merged_into INTEGER,
                    source_identity TEXT UNIQUE,
                    CHECK(channel IN ('offline','online'))
                );
                CREATE TABLE IF NOT EXISTS store_facts (
                    id INTEGER PRIMARY KEY, merchant_id INTEGER NOT NULL
                      REFERENCES store_merchants(id), mcc TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(merchant_id,mcc)
                );
                CREATE TABLE IF NOT EXISTS store_evidence (
                    id INTEGER PRIMARY KEY, fact_id INTEGER NOT NULL REFERENCES store_facts(id),
                    source TEXT NOT NULL, source_key TEXT NOT NULL, details_json TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0, UNIQUE(source,source_key)
                );
                CREATE TABLE IF NOT EXISTS store_sources (
                    id INTEGER PRIMARY KEY, source TEXT NOT NULL, store_id TEXT NOT NULL,
                    merchant_id INTEGER NOT NULL REFERENCES store_merchants(id),
                    network_id TEXT, metadata_json TEXT NOT NULL,
                    UNIQUE(source,store_id)
                );
                CREATE TABLE IF NOT EXISTS store_audit (
                    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, merchant_id INTEGER NOT NULL,
                    actor_id INTEGER NOT NULL, changes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, reverted_by INTEGER
                );
                CREATE TABLE IF NOT EXISTS store_import_checkpoints (
                    key TEXT PRIMARY KEY, value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS store_evidence_fact ON store_evidence(fact_id);
                CREATE INDEX IF NOT EXISTS store_audit_merchant ON store_audit(merchant_id,id);
            """)
        if not self._memory:
            os.chmod(self.path, 0o600)

    @staticmethod
    def _merchant(row: sqlite3.Row) -> Merchant:
        return Merchant(
            row["id"],
            row["name"],
            row["channel"],
            tuple(json.loads(row["aliases_json"])),
            bool(row["archived"]),
            row["revision"],
        )

    def get(self, merchant_id: int, *, connection=None, include_archived=False) -> Merchant | None:
        """Read a merchant; archived and merged records are hidden by default."""

        if connection is None:
            with self.connection() as conn:
                return self.get(merchant_id, connection=conn, include_archived=include_archived)
        row = connection.execute(
            "SELECT * FROM store_merchants WHERE id=?", (merchant_id,)
        ).fetchone()
        if row is None or (row["archived"] and not include_archived):
            return None
        return self._merchant(row)

    def search(self, query: str, *, limit=20, offset=0) -> SearchResult:
        """Search canonical names/aliases; fuzzy results never modify identity."""

        needle = normalize_store_name(query)
        if not needle:
            return SearchResult(())
        limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
        with self.connection() as connection:
            merchants = tuple(
                self._merchant(row)
                for row in connection.execute(
                    "SELECT * FROM store_merchants WHERE archived=0 ORDER BY name,id"
                )
            )
        exact, transliterated, partial, suggestions = [], [], [], []
        for merchant in merchants:
            names = (merchant.name, *merchant.aliases)
            keys = [normalize_store_name(name) for name in names]
            if needle in keys:
                exact.append(merchant)
            elif any(_relaxed_cross_script_match(query, name, needle) for name in names):
                transliterated.append(merchant)
            elif any(needle in key for key in keys):
                partial.append(merchant)
            elif len(needle) >= 3:
                score = max(SequenceMatcher(None, needle, key).ratio() for key in keys)
                if score >= 0.68:
                    suggestions.append((score, merchant))
        matches = exact + transliterated + partial
        fuzzy = tuple(
            item[1] for item in sorted(suggestions, key=lambda item: (-item[0], item[1].id))
        )
        return SearchResult(
            tuple(matches[offset : offset + limit]), fuzzy[:5] if not matches else (), len(matches)
        )

    def find_exact(self, name: str, channel: str, *, connection=None) -> tuple[Merchant, ...]:
        """Find possible exact manual duplicates without ever merging by name."""

        if connection is None:
            with self.connection() as conn:
                return self.find_exact(name, channel, connection=conn)
        needle = normalize_store_name(name)
        rows = connection.execute(
            "SELECT * FROM store_merchants WHERE archived=0 AND channel=? ORDER BY id",
            (_channel(channel),),
        )
        return tuple(
            self._merchant(row)
            for row in rows
            if needle
            in {
                normalize_store_name(item)
                for item in (row["name"], *json.loads(row["aliases_json"]))
            }
        )

    def list_mcc(
        self, merchant_id: int, *, connection=None, include_archived=False
    ) -> tuple[MccFact, ...]:
        """Return unique MCC facts with counts of live, independent evidence."""

        if connection is None:
            with self.connection() as conn:
                return self.list_mcc(
                    merchant_id, connection=conn, include_archived=include_archived
                )
        rows = connection.execute(
            """
            SELECT f.*, (SELECT count(*) FROM store_evidence e
              WHERE e.fact_id=f.id AND e.revoked=0) AS evidence_count
            FROM store_facts f WHERE merchant_id=? AND (? OR archived=0) ORDER BY mcc
        """,
            (merchant_id, include_archived),
        )
        return tuple(
            MccFact(
                row["merchant_id"],
                row["mcc"],
                bool(row["archived"]),
                row["revision"],
                row["evidence_count"],
            )
            for row in rows
        )

    def history(
        self, merchant_id=None, *, limit=20, offset: int = 0, connection=None
    ) -> tuple[AuditEntry, ...]:
        """Return newest-first audit pages without media or contribution text.

        ``limit`` is clamped to 1..100. Integer ``offset`` is clamped to
        0..1,000,000, including when sharing an existing transaction.
        """

        if isinstance(offset, bool) or not isinstance(offset, int):
            raise StoreError("Смещение истории должно быть целым числом")
        offset = max(0, min(offset, 1_000_000))
        if connection is None:
            with self.connection() as conn:
                return self.history(merchant_id, limit=limit, offset=offset, connection=conn)
        rows = connection.execute(
            """SELECT id,kind,merchant_id,actor_id,created_at,reverted_by,changes_json
            FROM store_audit WHERE (? IS NULL OR merchant_id=?)
            ORDER BY id DESC LIMIT ? OFFSET ?""",
            (merchant_id, merchant_id, max(1, min(int(limit), 100)), offset),
        )
        result = []
        for row in rows:
            values = dict(row)
            values["details"] = self._audit_details(connection, values.pop("changes_json"))
            result.append(AuditEntry(**values))
        return tuple(result)

    @staticmethod
    def _audit_details(connection: sqlite3.Connection, changes_json: str) -> tuple[str, ...]:
        """Summarize allowlisted audit fields without exposing raw evidence metadata."""

        def clipped(value: Any, maximum: int = 80) -> str:
            text = " ".join(str(value).split())
            return text if len(text) <= maximum else text[: maximum - 1] + "…"

        def aliases(raw: str | None) -> str:
            try:
                values = json.loads(raw or "[]")
            except (TypeError, json.JSONDecodeError):
                return "неизвестно"
            if not values:
                return "нет"
            shown = [f"«{clipped(value, 40)}»" for value in values[:3]]
            if len(values) > 3:
                shown.append(f"ещё {len(values) - 3}")
            return ", ".join(shown)

        def listed(values: list[str]) -> str:
            unique = list(dict.fromkeys(values))
            shown = unique[:8]
            suffix = f" и ещё {len(unique) - 8}" if len(unique) > 8 else ""
            return ", ".join(shown) + suffix

        try:
            changes = json.loads(changes_json)
        except (TypeError, json.JSONDecodeError):
            changes = []
        if not isinstance(changes, list):
            changes = []
        details: list[str] = []
        added_mcc: list[str] = []
        hidden_mcc: list[str] = []
        restored_mcc: list[str] = []
        evidence_added: dict[tuple[str, str], int] = {}
        evidence_revoked: dict[tuple[str, str], int] = {}
        source_added = 0
        source_updated = 0

        for edit in changes:
            if not isinstance(edit, dict):
                continue
            table = edit.get("table")
            before = edit.get("before")
            after = edit.get("after")
            if table == "store_merchants":
                if before is None and after:
                    channel = (
                        "онлайн/приложение"
                        if after.get("channel") == "online"
                        else "обычный магазин"
                    )
                    details.append(
                        f"Добавлен магазин «{clipped(after.get('name', ''))}» · {channel}."
                    )
                    continue
                if not before or not after:
                    continue
                if before.get("name") != after.get("name"):
                    details.append(
                        f"Название: «{clipped(before.get('name', ''))}» → "
                        f"«{clipped(after.get('name', ''))}»."
                    )
                if before.get("aliases_json") != after.get("aliases_json"):
                    details.append(
                        f"Другие названия: {aliases(before.get('aliases_json'))} → "
                        f"{aliases(after.get('aliases_json'))}."
                    )
                if before.get("archived") != after.get("archived"):
                    details.append(
                        "Магазин убран из поиска."
                        if after.get("archived")
                        else "Магазин возвращён в поиск."
                    )
                if before.get("merged_into") != after.get("merged_into") and after.get(
                    "merged_into"
                ):
                    target = connection.execute(
                        "SELECT name FROM store_merchants WHERE id=?", (after["merged_into"],)
                    ).fetchone()
                    target_name = (
                        f"«{clipped(target['name'])}»"
                        if target is not None
                        else f"записью №{after['merged_into']}"
                    )
                    details.append(f"Магазин объединён с {target_name}.")
            elif table == "store_facts":
                state = after or before or {}
                mcc = str(state.get("mcc", ""))
                if before is None and after:
                    added_mcc.append(mcc)
                elif before and after and before.get("archived") != after.get("archived"):
                    (hidden_mcc if after.get("archived") else restored_mcc).append(mcc)
            elif table == "store_evidence":
                state = after or before or {}
                fact = connection.execute(
                    "SELECT mcc FROM store_facts WHERE id=?", (state.get("fact_id"),)
                ).fetchone()
                mcc = fact["mcc"] if fact is not None else "неизвестный MCC"
                source = "tannei.by" if state.get("source") == "tannei" else "пользователь"
                key = source, mcc
                if before is None and after:
                    evidence_added[key] = evidence_added.get(key, 0) + 1
                elif before and after and not before.get("revoked") and after.get("revoked"):
                    evidence_revoked[key] = evidence_revoked.get(key, 0) + 1
            elif table == "store_sources":
                if before is None and after:
                    source_added += 1
                elif before and after and before != after:
                    source_updated += 1

        if added_mcc:
            details.append(f"Добавлен MCC: {listed(added_mcc)}.")
        if hidden_mcc:
            details.append(f"Убран из поиска MCC: {listed(hidden_mcc)}.")
        if restored_mcc:
            details.append(f"Возвращён MCC: {listed(restored_mcc)}.")
        for (source, mcc), count in list(evidence_added.items())[:5]:
            details.append(f"Подтверждения {source} для MCC {mcc}: +{count}.")
        for (source, mcc), count in list(evidence_revoked.items())[:5]:
            details.append(f"Отозваны подтверждения {source} для MCC {mcc}: {count}.")
        if source_added:
            details.append(f"Добавлены записи источника: {source_added}.")
        if source_updated:
            details.append(f"Обновлены записи источника: {source_updated}.")
        if not details:
            details.append("Обновлены служебные связи магазина без изменения видимых MCC.")
        return tuple(clipped(detail, 220) for detail in details[:10])

    @staticmethod
    def _required(connection, merchant_id):
        row = connection.execute(
            "SELECT * FROM store_merchants WHERE id=?", (merchant_id,)
        ).fetchone()
        if row is None or row["archived"]:
            raise StoreError("Магазин недоступен; откройте поиск заново")
        return row

    @staticmethod
    def _update(connection, changes, table, row_id, **values):
        changes.touch(table, row_id)
        connection.execute(
            f"UPDATE {table} SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
            (*values.values(), row_id),
        )

    @staticmethod
    def _insert(connection, changes, table, **values):
        cursor = connection.execute(
            f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )
        changes.touch(table, cursor.lastrowid, new=True)
        return cursor.lastrowid

    def _fact(self, connection, changes, merchant_id, mcc, *, reactivate=True):
        mcc = normalize_mcc(mcc)
        row = connection.execute(
            "SELECT * FROM store_facts WHERE merchant_id=? AND mcc=?", (merchant_id, mcc)
        ).fetchone()
        if row is None:
            return self._insert(
                connection, changes, "store_facts", merchant_id=merchant_id, mcc=mcc
            )
        if row["archived"] and reactivate:
            self._update(
                connection,
                changes,
                "store_facts",
                row["id"],
                archived=0,
                revision=row["revision"] + 1,
            )
        return row["id"]

    def _evidence(self, connection, changes, fact_id, evidence, *, source="community", key=None):
        if evidence is None:
            evidence = {}
        if not isinstance(evidence, dict):
            raise StoreError("Подтверждение должно быть объектом")
        # Media lives only in the expiring community-media table, never in audit snapshots.
        allowed = {
            "submission_id",
            "source",
            "payment_date",
            "address_extra",
            "merchant_type",
            "source_store_id",
            "source_network_id",
            "source_url",
            "occurrence",
        }
        if set(evidence) - allowed:
            raise StoreError("Подтверждение содержит неподдерживаемые поля")
        details = _json(evidence)
        if len(details) > 4000:
            raise StoreError("Подтверждение слишком длинное")
        if key is None:
            key = str(evidence.get("submission_id") or uuid.uuid4().hex)
        existing = connection.execute(
            "SELECT * FROM store_evidence WHERE source=? AND source_key=?", (source, key)
        ).fetchone()
        if existing:
            before_fact = changes.before.get(("store_facts", fact_id))
            if existing["fact_id"] != fact_id or (existing["revoked"] and source == "community"):
                raise StoreError("Подтверждение уже использовано или отменено")
            if source == "community" and before_fact and before_fact["archived"]:
                raise StoreError(
                    "MCC архивирован после этого подтверждения; нужно новое предложение"
                )
            return
        self._insert(
            connection,
            changes,
            "store_evidence",
            fact_id=fact_id,
            source=source,
            source_key=key,
            details_json=details,
        )

    def apply_change(
        self, kind: str, payload: dict, actor_id: int, *, connection=None
    ) -> ChangeResult:
        """Validate and atomically apply one audited edit; accept a review transaction.

        Supported kinds: add_merchant, add_mcc, replace_mcc, archive_mcc,
        rename_merchant, aliases, archive_merchant, merge_merchant, revert.
        Reversion creates a new audit and retains the historical evidence rows.
        """

        if connection is None:
            with self.transaction() as conn:
                return self.apply_change(kind, payload, actor_id, connection=conn)
        connection.execute("SAVEPOINT store_change")
        try:
            result = self._apply_change(kind, payload, actor_id, connection)
        except (KeyError, TypeError) as exc:
            connection.execute("ROLLBACK TO store_change")
            connection.execute("RELEASE store_change")
            raise StoreError("Изменение содержит неверные или неполные поля") from exc
        except BaseException:
            connection.execute("ROLLBACK TO store_change")
            connection.execute("RELEASE store_change")
            raise
        connection.execute("RELEASE store_change")
        return result

    def _apply_change(self, kind, payload, actor_id, connection):
        if not isinstance(payload, dict):
            raise StoreError("Изменение должно быть объектом")
        changes = _Changes(connection)
        reverted = None
        if kind == "revert":
            merchant_id, reverted = self._revert(connection, changes, payload["audit_id"])
        elif kind == "add_merchant":
            merchant_id = self._insert(
                connection,
                changes,
                "store_merchants",
                name=_name(payload["name"]),
                channel=_channel(payload.get("channel", "offline")),
                aliases_json=_json(_aliases(payload.get("aliases", []))),
            )
            if payload.get("mcc"):
                fact_id = self._fact(connection, changes, merchant_id, payload["mcc"])
                changes.touch("store_facts", fact_id)
                self._evidence(connection, changes, fact_id, payload.get("evidence"))
        else:
            merchant_id = int(payload["merchant_id"])
            merchant = self._required(connection, merchant_id)
            if kind in {"add_mcc", "replace_mcc"}:
                if kind == "replace_mcc":
                    if normalize_mcc(payload["old_mcc"]) == normalize_mcc(payload["mcc"]):
                        raise StoreError("Новый MCC совпадает с прежним")
                    self._archive_fact(connection, changes, merchant_id, payload["old_mcc"])
                fact_id = self._fact(connection, changes, merchant_id, payload["mcc"])
                self._evidence(connection, changes, fact_id, payload.get("evidence"))
                changes.touch("store_facts", fact_id)
            elif kind == "archive_mcc":
                self._archive_fact(connection, changes, merchant_id, payload["mcc"])
            elif kind == "rename_merchant":
                self._update(
                    connection,
                    changes,
                    "store_merchants",
                    merchant_id,
                    name=_name(payload["name"]),
                    revision=merchant["revision"] + 1,
                )
            elif kind == "aliases":
                self._update(
                    connection,
                    changes,
                    "store_merchants",
                    merchant_id,
                    aliases_json=_json(_aliases(payload["aliases"])),
                    revision=merchant["revision"] + 1,
                )
            elif kind == "archive_merchant":
                self._update(
                    connection,
                    changes,
                    "store_merchants",
                    merchant_id,
                    archived=1,
                    revision=merchant["revision"] + 1,
                )
            elif kind == "merge_merchant":
                self._merge(connection, changes, merchant, int(payload["target_id"]))
            else:
                raise StoreError("Неизвестное изменение")
        cursor = connection.execute(
            "INSERT INTO store_audit(kind,merchant_id,actor_id,changes_json) VALUES(?,?,?,?)",
            (kind, merchant_id, actor_id, _json(changes.finish())),
        )
        if reverted is not None:
            connection.execute(
                "UPDATE store_audit SET reverted_by=? WHERE id=?", (cursor.lastrowid, reverted)
            )
        return ChangeResult(cursor.lastrowid, merchant_id)

    def _archive_fact(self, connection, changes, merchant_id, mcc):
        row = connection.execute(
            "SELECT * FROM store_facts WHERE merchant_id=? AND mcc=? AND archived=0",
            (merchant_id, normalize_mcc(mcc)),
        ).fetchone()
        if row is None:
            raise StoreError("Такого действующего MCC у магазина нет")
        self._update(
            connection, changes, "store_facts", row["id"], archived=1, revision=row["revision"] + 1
        )

    def _merge(self, connection, changes, source, target_id):
        target = self._required(connection, target_id)
        if source["id"] == target_id or source["channel"] != target["channel"]:
            raise StoreError("Объединять можно только разные магазины одного канала")
        aliases = _aliases(
            [
                *json.loads(target["aliases_json"]),
                source["name"],
                *json.loads(source["aliases_json"]),
            ]
        )
        self._update(
            connection,
            changes,
            "store_merchants",
            target_id,
            aliases_json=_json(aliases),
            revision=target["revision"] + 1,
        )
        for fact in connection.execute(
            "SELECT * FROM store_facts WHERE merchant_id=?", (source["id"],)
        ).fetchall():
            existing = connection.execute(
                "SELECT * FROM store_facts WHERE merchant_id=? AND mcc=?", (target_id, fact["mcc"])
            ).fetchone()
            target_fact = self._fact(connection, changes, target_id, fact["mcc"], reactivate=False)
            if existing is None and fact["archived"]:
                self._update(connection, changes, "store_facts", target_fact, archived=1)
            for evidence in connection.execute(
                "SELECT * FROM store_evidence WHERE fact_id=?", (fact["id"],)
            ).fetchall():
                self._update(
                    connection, changes, "store_evidence", evidence["id"], fact_id=target_fact
                )
            if not fact["archived"]:
                self._update(
                    connection,
                    changes,
                    "store_facts",
                    fact["id"],
                    archived=1,
                    revision=fact["revision"] + 1,
                )
        for link in connection.execute(
            "SELECT * FROM store_sources WHERE merchant_id=?", (source["id"],)
        ).fetchall():
            self._update(connection, changes, "store_sources", link["id"], merchant_id=target_id)
        self._update(
            connection,
            changes,
            "store_merchants",
            source["id"],
            archived=1,
            merged_into=target_id,
            revision=source["revision"] + 1,
        )

    def _revert(self, connection, changes, audit_id):
        audit = connection.execute("SELECT * FROM store_audit WHERE id=?", (audit_id,)).fetchone()
        if audit is None or audit["reverted_by"] is not None or audit["kind"] == "revert":
            raise StaleChangeError("Изменение уже отменено или недоступно для отмены")
        edits = json.loads(audit["changes_json"])
        if audit["kind"] == "merge_merchant":
            merchant_ids = [edit["id"] for edit in edits if edit["table"] == "store_merchants"]
            placeholders = ",".join("?" for _ in merchant_ids)
            if connection.execute(
                "SELECT 1 FROM store_audit WHERE kind='import' AND id>? "
                f"AND merchant_id IN ({placeholders}) LIMIT 1",
                (audit["id"], *merchant_ids),
            ).fetchone():
                raise StaleChangeError(
                    "После объединения появились импортированные данные; "
                    "внесите отдельное исправление"
                )
        for edit in edits:
            row = connection.execute(
                f"SELECT * FROM {edit['table']} WHERE id=?", (edit["id"],)
            ).fetchone()
            if (dict(row) if row else None) != edit["after"]:
                raise StaleChangeError(
                    "Есть более поздние изменения; внесите отдельное исправление"
                )
        # Revoke/rehome evidence before deciding whether an association still has support.
        order = {"store_evidence": 0, "store_sources": 1, "store_facts": 2, "store_merchants": 3}
        for edit in sorted(edits, key=lambda item: order[item["table"]]):
            table, row_id, before, after = edit["table"], edit["id"], edit["before"], edit["after"]
            if table == "store_evidence" and before is None:
                self._update(connection, changes, table, row_id, revoked=1)
            elif table == "store_facts":
                supported = connection.execute(
                    "SELECT 1 FROM store_evidence WHERE fact_id=? AND revoked=0", (row_id,)
                ).fetchone()
                archived = before["archived"] if before else 1
                if not supported:
                    archived = 1
                if archived and not after["archived"] and supported:
                    continue
                self._update(
                    connection,
                    changes,
                    table,
                    row_id,
                    archived=archived,
                    revision=after["revision"] + 1,
                )
            elif table == "store_merchants" and before is None:
                if connection.execute(
                    "SELECT 1 FROM store_facts WHERE merchant_id=? AND archived=0", (row_id,)
                ).fetchone():
                    raise StaleChangeError("У магазина уже есть независимые подтверждения")
                self._update(
                    connection, changes, table, row_id, archived=1, revision=after["revision"] + 1
                )
            elif before is None:
                # Source identities are permanent. Imported history cannot be deleted.
                raise StaleChangeError(
                    "Импортированные связи сохраняются; используйте архивирование"
                )
            else:
                values = {key: value for key, value in before.items() if key != "id"}
                if "revision" in values:
                    values["revision"] = after["revision"] + 1
                self._update(connection, changes, table, row_id, **values)
        return audit["merchant_id"], audit["id"]

    def checkpoint(self, key: str, value=None, *, write=False):
        """Read or atomically persist a resumable importer checkpoint."""

        with self.transaction() if write else self.connection() as connection:
            if write:
                connection.execute(
                    """INSERT INTO store_import_checkpoints(key,value_json) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                    updated_at=CURRENT_TIMESTAMP""",
                    (key, _json(value)),
                )
                return value
            row = connection.execute(
                "SELECT value_json FROM store_import_checkpoints WHERE key=?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def import_store(self, metadata: dict, observations: list[dict]) -> ChangeResult:
        """Import validated tannei metadata/evidence idempotently, retaining manual edits.

        Offline stores group only by exact source network ID. Online applications
        group by exact source store ID, even when attached to an offline network.
        Repeated import never renames, unarchives, or undoes a moderator merge.
        """

        with self.transaction() as connection:
            changes = _Changes(connection)
            source_id, network_id = str(metadata["id"]), metadata.get("network_id")
            identity = (
                f"tannei:online:{source_id}"
                if metadata["is_online"]
                else f"tannei:network:{network_id}"
                if network_id
                else f"tannei:store:{source_id}"
            )
            link = connection.execute(
                "SELECT * FROM store_sources WHERE source='tannei' AND store_id=?", (source_id,)
            ).fetchone()
            if link:
                previous = json.loads(link["metadata_json"])
                if any(previous[key] != metadata[key] for key in ("network_id", "is_online")):
                    raise StoreError(
                        "Source store changed network/channel; explicit review is required"
                    )
            merchant = connection.execute(
                "SELECT * FROM store_merchants WHERE source_identity=?", (identity,)
            ).fetchone()
            merchant_id = link["merchant_id"] if link else merchant["id"] if merchant else None
            # Follow moderated source-identity merges for newly discovered branches too.
            while merchant_id is not None:
                current = connection.execute(
                    "SELECT * FROM store_merchants WHERE id=?", (merchant_id,)
                ).fetchone()
                if not current["merged_into"]:
                    break
                merchant_id = current["merged_into"]
            if merchant_id is None:
                name = (
                    metadata["name"]
                    if metadata["is_online"]
                    else metadata.get("network_name") or metadata["name"]
                )
                merchant_id = self._insert(
                    connection,
                    changes,
                    "store_merchants",
                    name=_name(name),
                    channel="online" if metadata["is_online"] else "offline",
                    source_identity=identity,
                )
            if link is None:
                self._insert(
                    connection,
                    changes,
                    "store_sources",
                    source="tannei",
                    store_id=source_id,
                    merchant_id=merchant_id,
                    network_id=str(network_id) if network_id else None,
                    metadata_json=_json(metadata),
                )
            elif link["metadata_json"] != _json(metadata):
                self._update(
                    connection, changes, "store_sources", link["id"], metadata_json=_json(metadata)
                )
            occurrences: dict[str, int] = {}
            for observation in observations:
                digest = hashlib.sha256(_json(observation).encode()).hexdigest()
                occurrence = occurrences.get(digest, 0)
                occurrences[digest] = occurrence + 1
                fact_id = self._fact(
                    connection, changes, merchant_id, observation["mcc"], reactivate=False
                )
                evidence = {key: value for key, value in observation.items() if key != "mcc"}
                evidence.update(
                    source_store_id=source_id,
                    source_network_id=network_id,
                    source_url=f"https://tannei.by/api/v2/moneyback/stores/item/{source_id}/mcc/",
                    occurrence=occurrence,
                )
                self._evidence(
                    connection,
                    changes,
                    fact_id,
                    evidence,
                    source="tannei",
                    key=f"{source_id}:{digest}:{occurrence}",
                )
            edits = changes.finish()
            if not edits:
                return ChangeResult(0, merchant_id)
            cursor = connection.execute(
                "INSERT INTO store_audit(kind,merchant_id,actor_id,changes_json) "
                "VALUES('import',?,0,?)",
                (merchant_id, _json(edits)),
            )
            return ChangeResult(cursor.lastrowid, merchant_id)

    def counts(self) -> dict[str, int]:
        """Return non-personal operational directory/import counters."""

        with self.connection() as connection:
            return {
                name: connection.execute(sql).fetchone()[0]
                for name, sql in {
                    "merchants": "SELECT count(*) FROM store_merchants WHERE archived=0",
                    "mcc_facts": "SELECT count(*) FROM store_facts f "
                    "JOIN store_merchants m ON m.id=f.merchant_id "
                    "WHERE f.archived=0 AND m.archived=0",
                    "source_stores": "SELECT count(*) FROM store_sources WHERE source='tannei'",
                    "evidence": "SELECT count(*) FROM store_evidence WHERE revoked=0",
                }.items()
            }
