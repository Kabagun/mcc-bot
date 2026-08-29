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
from datetime import UTC, datetime
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
class Brand:
    """One user-facing brand that can contain several channel merchants."""

    id: int
    name: str
    aliases: tuple[str, ...]
    archived: bool
    revision: int


@dataclass(frozen=True, slots=True)
class MccFact:
    """A unique MCC association, supported by independent evidence records."""

    merchant_id: int
    mcc: str
    note: str
    archived: bool
    revision: int
    evidence_count: int


@dataclass(frozen=True, slots=True)
class BrandMccFact:
    """One channel-level MCC view aggregated without rewriting source facts."""

    channel: str
    mcc: str
    note: str
    merchant_ids: tuple[int, ...]
    evidence_count: int


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Deterministic matches and separately labelled fuzzy suggestions."""

    matches: tuple[Brand, ...]
    suggestions: tuple[Brand, ...] = ()
    total: int = 0


@dataclass(frozen=True, slots=True)
class ChangeResult:
    """Durable change identity returned to moderation."""

    audit_id: int
    merchant_id: int
    brand_id: int | None = None
    changed: bool = True


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """Privacy-safe historical change summary, without contributor evidence."""

    id: int
    kind: str
    merchant_id: int
    brand_id: int | None
    actor_id: int
    created_at: str
    reverted_by: int | None
    details: tuple[str, ...]
    summary: str


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


def _note(value: Any) -> str:
    """Validate a short public MCC note; an empty note is meaningful."""

    if not isinstance(value, str) or len(value.strip()) > 48:
        raise StoreError("Примечание должно содержать не более 48 символов")
    value = value.strip()
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise StoreError("Примечание содержит недопустимые символы")
    return value


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
        """Create and idempotently migrate the private durable schema.

        The brand migration is additive: every pre-brand merchant receives its
        own brand and membership while all existing merchant, source, fact,
        evidence and audit row IDs remain untouched.
        """

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
                    note TEXT NOT NULL DEFAULT '',
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
                CREATE TABLE IF NOT EXISTS store_tannei_snapshots (
                    id INTEGER PRIMARY KEY, brand_id INTEGER NOT NULL UNIQUE
                      REFERENCES store_brands(id),
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS store_tannei_import_guards (
                    id INTEGER PRIMARY KEY, store_source_id INTEGER NOT NULL UNIQUE
                      REFERENCES store_sources(id),
                    source_identity TEXT NOT NULL, revision INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL, last_legacy_audit_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS store_tannei_merge_guards (
                    audit_id INTEGER PRIMARY KEY, guards_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS store_evidence_fact ON store_evidence(fact_id);
                CREATE INDEX IF NOT EXISTS store_audit_merchant ON store_audit(merchant_id,id);
            """)
            connection.execute("BEGIN IMMEDIATE")
            try:
                fact_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(store_facts)")
                }
                if "note" not in fact_columns:
                    connection.execute(
                        "ALTER TABLE store_facts ADD COLUMN note TEXT NOT NULL DEFAULT ''"
                    )
                audit_columns = {
                    row["name"] for row in connection.execute("PRAGMA table_info(store_audit)")
                }
                if "brand_id" not in audit_columns:
                    connection.execute("ALTER TABLE store_audit ADD COLUMN brand_id INTEGER")
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS store_brands (
                        id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                        aliases_json TEXT NOT NULL DEFAULT '[]',
                        archived INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1, merged_into INTEGER
                          REFERENCES store_brands(id)
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS store_brand_members (
                        id INTEGER PRIMARY KEY, brand_id INTEGER NOT NULL
                          REFERENCES store_brands(id),
                        merchant_id INTEGER NOT NULL UNIQUE
                          REFERENCES store_merchants(id),
                        UNIQUE(brand_id,merchant_id)
                    )"""
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS store_brand_members_brand "
                    "ON store_brand_members(brand_id,merchant_id)"
                )
                for merchant in connection.execute(
                    """SELECT m.* FROM store_merchants m
                    LEFT JOIN store_brand_members bm ON bm.merchant_id=m.id
                    WHERE bm.id IS NULL ORDER BY m.id"""
                ).fetchall():
                    brand = connection.execute(
                        "SELECT id FROM store_brands WHERE id=?", (merchant["id"],)
                    ).fetchone()
                    occupied = (
                        connection.execute(
                            "SELECT 1 FROM store_brand_members WHERE brand_id=?",
                            (merchant["id"],),
                        ).fetchone()
                        if brand is not None
                        else None
                    )
                    if brand is None:
                        connection.execute(
                            """INSERT INTO store_brands
                            (id,name,aliases_json,archived,revision)
                            VALUES(?,?,?,?,?)""",
                            (
                                merchant["id"],
                                merchant["name"],
                                merchant["aliases_json"],
                                merchant["archived"],
                                merchant["revision"],
                            ),
                        )
                        brand_id = merchant["id"]
                    elif occupied is None:
                        brand_id = brand["id"]
                    else:
                        brand_id = connection.execute(
                            """INSERT INTO store_brands
                            (name,aliases_json,archived,revision) VALUES(?,?,?,?)""",
                            (
                                merchant["name"],
                                merchant["aliases_json"],
                                merchant["archived"],
                                merchant["revision"],
                            ),
                        ).lastrowid
                    connection.execute(
                        "INSERT INTO store_brand_members(brand_id,merchant_id) VALUES(?,?)",
                        (brand_id, merchant["id"]),
                    )
                connection.execute(
                    """UPDATE store_audit SET brand_id=(
                        SELECT bm.brand_id FROM store_brand_members bm
                        WHERE bm.merchant_id=store_audit.merchant_id
                    ) WHERE brand_id IS NULL"""
                )
                self._compact_tannei_state(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
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

    @staticmethod
    def _brand(row: sqlite3.Row) -> Brand:
        return Brand(
            row["id"],
            row["name"],
            tuple(json.loads(row["aliases_json"])),
            bool(row["archived"]),
            row["revision"],
        )

    @staticmethod
    def _snapshot_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        """Decode the private, canonical per-brand Tannei source snapshot."""

        if row is None:
            return {"stores": {}}
        try:
            payload = json.loads(row["snapshot_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StoreError("Снимок tannei.by повреждён") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("stores"), dict):
            raise StoreError("Снимок tannei.by повреждён")
        return payload

    @staticmethod
    def _merge_snapshot_payloads(*payloads: dict[str, Any]) -> dict[str, Any]:
        """Union immutable source observations without dropping earlier support."""

        stores: dict[str, dict[str, Any]] = {}
        for payload in payloads:
            for store_id, incoming in payload.get("stores", {}).items():
                store_id = str(store_id)
                if not isinstance(incoming, dict) or incoming.get("channel") not in {
                    "offline",
                    "online",
                }:
                    raise StoreError("Снимок tannei.by повреждён")
                observations = incoming.get("observations", {})
                if not isinstance(observations, dict):
                    raise StoreError("Снимок tannei.by повреждён")
                current = stores.get(store_id)
                if current is None:
                    stores[store_id] = {
                        "channel": incoming["channel"],
                        "observations": dict(observations),
                    }
                    continue
                if current["channel"] != incoming["channel"]:
                    raise StoreError("Источник сменил канал; требуется ручная проверка")
                for key, observation in observations.items():
                    if (
                        key in current["observations"]
                        and current["observations"][key] != observation
                    ):
                        raise StoreError(
                            "Наблюдение источника изменилось; требуется ручная проверка"
                        )
                    current["observations"][key] = observation
        return {"stores": {key: stores[key] for key in sorted(stores, key=lambda item: int(item))}}

    @staticmethod
    def _tannei_identity(metadata: dict[str, Any]) -> str:
        source_id = str(metadata["id"])
        network_id = metadata.get("network_id")
        return (
            f"tannei:online:{source_id}"
            if metadata["is_online"]
            else f"tannei:network:{network_id}"
            if network_id
            else f"tannei:store:{source_id}"
        )

    def _record_merge_guard(
        self, connection, audit_id, edits, *, legacy_imports: dict[int, list[int]] | None = None
    ) -> None:
        """Persist source revisions observed by a merchant merge for durable undo checks."""

        identities = {
            state["source_identity"]
            for edit in edits
            if isinstance(edit, dict) and edit.get("table") == "store_merchants"
            for state in (edit.get("before"),)
            if state and state.get("source_identity")
        }
        touched_source_ids = {
            int(edit["id"])
            for edit in edits
            if isinstance(edit, dict)
            and edit.get("table") == "store_sources"
            and (edit.get("before") or edit.get("after") or {}).get("source") == "tannei"
        }
        guarded: dict[str, dict[str, int]] = {}
        for identity in sorted(identities):
            values: dict[str, int] = {}
            for row in connection.execute(
                """SELECT * FROM store_tannei_import_guards
                WHERE source_identity=? ORDER BY id""",
                (identity,),
            ):
                revision = row["revision"]
                if legacy_imports is not None:
                    event_ids = legacy_imports.get(row["store_source_id"], [])
                    visible_events = [value for value in event_ids if value <= audit_id]
                    if visible_events:
                        revision = max(1, len(visible_events))
                    elif row["store_source_id"] not in touched_source_ids:
                        continue
                values[str(row["id"])] = revision
            guarded[identity] = values
        connection.execute(
            "INSERT OR IGNORE INTO store_tannei_merge_guards(audit_id,guards_json) VALUES(?,?)",
            (audit_id, _json(guarded)),
        )

    @staticmethod
    def _merge_guard_is_stale(connection, audit_id) -> bool:
        row = connection.execute(
            "SELECT guards_json FROM store_tannei_merge_guards WHERE audit_id=?", (audit_id,)
        ).fetchone()
        if row is None:
            return False
        try:
            expected = json.loads(row["guards_json"])
        except (TypeError, json.JSONDecodeError):
            return True
        for identity, revisions in expected.items():
            current = {
                str(guard["id"]): guard["revision"]
                for guard in connection.execute(
                    """SELECT id,revision FROM store_tannei_import_guards
                    WHERE source_identity=? ORDER BY id""",
                    (identity,),
                )
            }
            if current != revisions:
                return True
        return False

    @staticmethod
    def _public_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        """Aggregate a private source ledger into the stable public snapshot contract."""

        payload = StoreRepository._snapshot_payload(row)
        channels: dict[str, dict[str, dict[str, Any]]] = {"offline": {}, "online": {}}
        for store_id, source in payload["stores"].items():
            channel = source["channel"]
            for observation in source["observations"].values():
                if observation.get("revoked"):
                    continue
                mcc = normalize_mcc(observation.get("mcc"))
                state = channels[channel].setdefault(
                    mcc,
                    {
                        "support_count": 1,
                        "first_seen": None,
                        "last_seen": None,
                        "source_store_ids": [],
                    },
                )
                if store_id not in state["source_store_ids"]:
                    state["source_store_ids"].append(store_id)
                seen = observation.get("payment_date")
                if isinstance(seen, str) and seen:
                    state["first_seen"] = min(filter(None, (state["first_seen"], seen)))
                    state["last_seen"] = max(filter(None, (state["last_seen"], seen)))
        for values in channels.values():
            for state in values.values():
                state["source_store_ids"].sort(key=int)
        return {
            "brand_id": row["brand_id"],
            "revision": row["revision"],
            "updated_at": row["updated_at"],
            "source_count": len(payload["stores"]),
            "channels": channels,
        }

    def tannei_snapshot(self, brand_id: int, *, connection=None) -> dict[str, Any] | None:
        """Return the aggregate Tannei snapshot for one brand, if it has source rows."""

        if connection is None:
            with self.connection() as conn:
                return self.tannei_snapshot(brand_id, connection=conn)
        row = connection.execute(
            """SELECT s.* FROM store_tannei_snapshots s
            JOIN store_brands b ON b.id=s.brand_id
            WHERE s.brand_id=? AND b.archived=0""",
            (int(brand_id),),
        ).fetchone()
        return self._public_snapshot(row) if row is not None else None

    def brand_has_tannei(self, brand_id: int, *, connection=None) -> bool:
        """Report whether a brand is backed by any publicly imported Tannei source."""

        snapshot = self.tannei_snapshot(brand_id, connection=connection)
        return bool(snapshot and snapshot["source_count"])

    def brand_mcc_has_tannei(
        self, brand_id: int, channel: str, mcc: str, *, connection=None
    ) -> bool:
        """Report whether Tannei supports a specific public brand/channel/MCC fact."""

        snapshot = self.tannei_snapshot(brand_id, connection=connection)
        return bool(
            snapshot
            and normalize_mcc(mcc) in snapshot["channels"][_channel(channel)]
            and snapshot["channels"][_channel(channel)][normalize_mcc(mcc)]["support_count"]
        )

    def _compact_tannei_state(self, connection: sqlite3.Connection) -> None:
        """Migrate row-per-observation imports to one idempotent snapshot per brand.

        Human audit IDs and reversal links are never rebuilt. Legacy automated
        import audits are removed only when they do not already describe the
        compact snapshot representation. Legacy evidence rows referenced by a
        human audit remain as non-counting tombstones so that audit payloads,
        IDs and reversal semantics stay intact.
        """

        snapshots: dict[int, dict[str, Any]] = {}
        for row in connection.execute(
            """SELECT s.store_id,s.merchant_id,m.channel,bm.brand_id,s.metadata_json
            FROM store_sources s
            JOIN store_merchants m ON m.id=s.merchant_id
            JOIN store_brand_members bm ON bm.merchant_id=s.merchant_id
            WHERE s.source='tannei' ORDER BY s.id"""
        ).fetchall():
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            channel = "online" if metadata.get("is_online") else row["channel"]
            snapshots.setdefault(row["brand_id"], {"stores": {}})["stores"].setdefault(
                str(row["store_id"]), {"channel": channel, "observations": {}}
            )

        for row in connection.execute(
            """SELECT e.*,f.mcc,m.channel,bm.brand_id
            FROM store_evidence e
            JOIN store_facts f ON f.id=e.fact_id
            JOIN store_merchants m ON m.id=f.merchant_id
            JOIN store_brand_members bm ON bm.merchant_id=m.id
            WHERE e.source='tannei' ORDER BY e.id"""
        ).fetchall():
            try:
                details = json.loads(row["details_json"])
            except (TypeError, json.JSONDecodeError):
                details = {}
            source_id = str(details.get("source_store_id") or row["source_key"].split(":", 1)[0])
            observation = {
                key: value
                for key, value in details.items()
                if key
                not in {
                    "source_store_id",
                    "source_network_id",
                    "source_url",
                    "occurrence",
                }
            }
            observation["mcc"] = row["mcc"]
            if row["revoked"]:
                observation["revoked"] = True
            source = snapshots.setdefault(row["brand_id"], {"stores": {}})["stores"].setdefault(
                source_id, {"channel": row["channel"], "observations": {}}
            )
            source["observations"][row["source_key"]] = observation

        now = datetime.now(UTC).isoformat(timespec="seconds")
        for brand_id, incoming in snapshots.items():
            row = connection.execute(
                "SELECT * FROM store_tannei_snapshots WHERE brand_id=?", (brand_id,)
            ).fetchone()
            merged = self._merge_snapshot_payloads(self._snapshot_payload(row), incoming)
            encoded = _json(merged)
            if row is None:
                connection.execute(
                    """INSERT INTO store_tannei_snapshots
                    (brand_id,revision,updated_at,snapshot_json) VALUES(?,1,?,?)""",
                    (brand_id, now, encoded),
                )
            elif row["snapshot_json"] != encoded:
                connection.execute(
                    """UPDATE store_tannei_snapshots SET revision=revision+1,
                    updated_at=?,snapshot_json=? WHERE id=?""",
                    (now, encoded, row["id"]),
                )

        legacy_imports: dict[int, list[int]] = {}
        audit_rows = connection.execute(
            "SELECT id,kind,changes_json FROM store_audit ORDER BY id"
        ).fetchall()
        for audit in audit_rows:
            if audit["kind"] != "import":
                continue
            try:
                edits = json.loads(audit["changes_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            source_ids: set[int] = set()
            for edit in edits if isinstance(edits, list) else ():
                if not isinstance(edit, dict):
                    continue
                state = edit.get("after") or edit.get("before") or {}
                if edit.get("table") == "store_sources" and state.get("source") == "tannei":
                    source_ids.add(int(edit["id"]))
                elif edit.get("table") == "store_evidence" and state.get("source") == "tannei":
                    try:
                        details = json.loads(state.get("details_json") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        details = {}
                    source_store_id = details.get("source_store_id")
                    if source_store_id is not None:
                        link = connection.execute(
                            """SELECT id FROM store_sources
                            WHERE source='tannei' AND store_id=?""",
                            (str(source_store_id),),
                        ).fetchone()
                        if link is not None:
                            source_ids.add(link["id"])
            for source_link_id in source_ids:
                legacy_imports.setdefault(source_link_id, []).append(audit["id"])

        for source in connection.execute(
            "SELECT * FROM store_sources WHERE source='tannei' ORDER BY id"
        ).fetchall():
            if connection.execute(
                "SELECT 1 FROM store_tannei_import_guards WHERE store_source_id=?",
                (source["id"],),
            ).fetchone():
                continue
            metadata = json.loads(source["metadata_json"])
            identity = self._tannei_identity(metadata)
            brand_id = self._brand_id_for_merchant(connection, source["merchant_id"])
            snapshot_row = connection.execute(
                "SELECT * FROM store_tannei_snapshots WHERE brand_id=?", (brand_id,)
            ).fetchone()
            source_payload = self._snapshot_payload(snapshot_row)["stores"].get(
                str(source["store_id"]),
                {
                    "channel": "online" if metadata.get("is_online") else "offline",
                    "observations": {},
                },
            )
            fingerprint = hashlib.sha256(
                f"{source['metadata_json']}\n{_json(source_payload)}".encode()
            ).hexdigest()
            import_ids = legacy_imports.get(source["id"], [])
            connection.execute(
                """INSERT INTO store_tannei_import_guards
                (store_source_id,source_identity,revision,fingerprint,last_legacy_audit_id)
                VALUES(?,?,?,?,?)""",
                (
                    source["id"],
                    identity,
                    max(1, len(import_ids)),
                    fingerprint,
                    max(import_ids) if import_ids else None,
                ),
            )

        for audit in audit_rows:
            if (
                audit["kind"] != "merge_merchant"
                or connection.execute(
                    "SELECT 1 FROM store_tannei_merge_guards WHERE audit_id=?", (audit["id"],)
                ).fetchone()
            ):
                continue
            self._record_merge_guard(
                connection,
                audit["id"],
                json.loads(audit["changes_json"]),
                legacy_imports=legacy_imports,
            )

        legacy_tannei_ids = {
            row["id"]
            for row in connection.execute("SELECT id FROM store_evidence WHERE source='tannei'")
        }
        if legacy_tannei_ids:
            protected_ids: set[int] = set()
            for audit in audit_rows:
                try:
                    edits = json.loads(audit["changes_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(edits, list):
                    continue
                if audit["kind"] != "import":
                    protected_ids.update(
                        edit["id"]
                        for edit in edits
                        if isinstance(edit, dict)
                        and edit.get("table") == "store_evidence"
                        and edit.get("id") in legacy_tannei_ids
                    )
            removable = legacy_tannei_ids - protected_ids
            if removable:
                connection.execute(
                    f"DELETE FROM store_evidence WHERE id IN ({','.join('?' for _ in removable)})",
                    tuple(sorted(removable)),
                )

        for audit in connection.execute(
            "SELECT id,changes_json FROM store_audit WHERE kind='import'"
        ).fetchall():
            try:
                edits = json.loads(audit["changes_json"])
            except (TypeError, json.JSONDecodeError):
                edits = []
            if not any(
                isinstance(edit, dict) and edit.get("table") == "store_tannei_snapshots"
                for edit in edits
            ):
                connection.execute("DELETE FROM store_audit WHERE id=?", (audit["id"],))

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

    def get_brand(self, brand_id: int, *, connection=None, include_archived=False) -> Brand | None:
        """Read a brand; merged and archived brands are hidden by default."""

        if connection is None:
            with self.connection() as conn:
                return self.get_brand(brand_id, connection=conn, include_archived=include_archived)
        row = connection.execute("SELECT * FROM store_brands WHERE id=?", (brand_id,)).fetchone()
        if row is None or (row["archived"] and not include_archived):
            return None
        return self._brand(row)

    def brand_for_merchant(
        self, merchant_id: int, *, connection=None, include_archived=False
    ) -> Brand | None:
        """Return the current brand membership for a merchant."""

        if connection is None:
            with self.connection() as conn:
                return self.brand_for_merchant(
                    merchant_id, connection=conn, include_archived=include_archived
                )
        row = connection.execute(
            """SELECT b.* FROM store_brands b
            JOIN store_brand_members bm ON bm.brand_id=b.id
            WHERE bm.merchant_id=?""",
            (merchant_id,),
        ).fetchone()
        if row is None or (row["archived"] and not include_archived):
            return None
        return self._brand(row)

    def list_brand_members(
        self,
        brand_id: int,
        *,
        channel: str | None = None,
        connection=None,
        include_archived=False,
    ) -> tuple[Merchant, ...]:
        """List durable merchant branches belonging to one brand."""

        if connection is None:
            with self.connection() as conn:
                return self.list_brand_members(
                    brand_id,
                    channel=channel,
                    connection=conn,
                    include_archived=include_archived,
                )
        if channel is not None:
            channel = _channel(channel)
        rows = connection.execute(
            """SELECT m.* FROM store_merchants m
            JOIN store_brand_members bm ON bm.merchant_id=m.id
            WHERE bm.brand_id=? AND (? IS NULL OR m.channel=?)
              AND (? OR m.archived=0)
            ORDER BY CASE m.channel WHEN 'offline' THEN 0 ELSE 1 END,m.id""",
            (brand_id, channel, channel, include_archived),
        )
        return tuple(self._merchant(row) for row in rows)

    def list_brand_channels(
        self, brand_id: int, *, connection=None, include_archived=False
    ) -> dict[str, tuple[Merchant, ...]]:
        """Group a brand's merchant branches by payment channel."""

        if connection is None:
            with self.connection() as conn:
                return self.list_brand_channels(
                    brand_id, connection=conn, include_archived=include_archived
                )
        members = self.list_brand_members(
            brand_id, connection=connection, include_archived=include_archived
        )
        return {
            channel: tuple(member for member in members if member.channel == channel)
            for channel in ("offline", "online")
            if any(member.channel == channel for member in members)
        }

    def search(self, query: str, *, limit=20, offset=0) -> SearchResult:
        """Search brands in strict tiers; fuzzy results never modify identity.

        The first non-empty tier wins globally: exact official name/alias,
        whole-name cross-script pronunciation, partial, then suggestions. This
        prevents an exact ``Мила`` result from being diluted by ``Милавица``.
        """

        needle = normalize_store_name(query)
        if not needle:
            return SearchResult(())
        limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
        with self.connection() as connection:
            brands = tuple(
                self._brand(row)
                for row in connection.execute(
                    """SELECT b.* FROM store_brands b WHERE b.archived=0
                    AND EXISTS (
                      SELECT 1 FROM store_brand_members bm
                      JOIN store_merchants m ON m.id=bm.merchant_id
                      WHERE bm.brand_id=b.id AND m.archived=0
                    ) ORDER BY b.name,b.id"""
                )
            )
            member_names: dict[int, list[str]] = {}
            for row in connection.execute(
                """SELECT bm.brand_id,m.name,m.aliases_json
                FROM store_brand_members bm JOIN store_merchants m ON m.id=bm.merchant_id
                WHERE m.archived=0
                ORDER BY bm.brand_id,m.id"""
            ):
                member_names.setdefault(row["brand_id"], []).extend(
                    (row["name"], *json.loads(row["aliases_json"]))
                )
        exact, transliterated, partial, suggestions = [], [], [], []
        for brand in brands:
            names = tuple(
                dict.fromkeys((brand.name, *brand.aliases, *member_names.get(brand.id, [])))
            )
            keys = [normalize_store_name(name) for name in names]
            if needle in keys:
                exact.append(brand)
            elif any(_relaxed_cross_script_match(query, name, needle) for name in names):
                transliterated.append(brand)
            elif any(needle in key for key in keys):
                partial.append(brand)
            elif len(needle) >= 3:
                score = max(SequenceMatcher(None, needle, key).ratio() for key in keys)
                if score >= 0.68:
                    suggestions.append((score, brand))
        matches = exact or transliterated or partial
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
              WHERE e.fact_id=f.id AND e.revoked=0 AND e.source<>'tannei') AS evidence_count
            FROM store_facts f WHERE merchant_id=? AND (? OR archived=0) ORDER BY mcc
        """,
            (merchant_id, include_archived),
        ).fetchall()
        brand_id = self._brand_id_for_merchant(connection, merchant_id)
        support = self._tannei_support_by_merchant(connection, brand_id)
        return tuple(
            MccFact(
                row["merchant_id"],
                row["mcc"],
                row["note"],
                bool(row["archived"]),
                row["revision"],
                row["evidence_count"] + support.get((merchant_id, row["mcc"]), 0),
            )
            for row in rows
        )

    def _tannei_support_by_merchant(
        self, connection: sqlite3.Connection, brand_id: int
    ) -> dict[tuple[int, str], int]:
        """Assign one logical Tannei confirmation to each public channel/MCC fact."""

        row = connection.execute(
            "SELECT * FROM store_tannei_snapshots WHERE brand_id=?", (brand_id,)
        ).fetchone()
        if row is None:
            return {}
        result: dict[tuple[int, str], int] = {}
        public = self._public_snapshot(row)
        for channel, facts in public["channels"].items():
            for mcc in facts:
                owner = connection.execute(
                    """SELECT f.merchant_id FROM store_brand_members bm
                    JOIN store_merchants m ON m.id=bm.merchant_id
                    JOIN store_facts f ON f.merchant_id=m.id
                    WHERE bm.brand_id=? AND m.channel=? AND f.mcc=?
                    ORDER BY m.archived,f.archived,f.id LIMIT 1""",
                    (brand_id, channel, mcc),
                ).fetchone()
                if owner is not None:
                    result[(owner["merchant_id"], mcc)] = 1
        return result

    def list_brand_mcc(
        self,
        brand_id: int,
        *,
        channel: str | None = None,
        connection=None,
        include_archived=False,
    ) -> tuple[BrandMccFact, ...]:
        """Return channel-level MCC groups without collapsing source fact rows."""

        if connection is None:
            with self.connection() as conn:
                return self.list_brand_mcc(
                    brand_id,
                    channel=channel,
                    connection=conn,
                    include_archived=include_archived,
                )
        if channel is not None:
            channel = _channel(channel)
        rows = connection.execute(
            """SELECT m.channel,f.mcc,f.note,f.merchant_id,
              (SELECT count(*) FROM store_evidence e
               WHERE e.fact_id=f.id AND e.revoked=0 AND e.source<>'tannei') AS evidence_count
            FROM store_brand_members bm
            JOIN store_merchants m ON m.id=bm.merchant_id
            JOIN store_facts f ON f.merchant_id=m.id
            WHERE bm.brand_id=? AND m.archived=0
              AND (? IS NULL OR m.channel=?) AND (? OR f.archived=0)
            ORDER BY CASE m.channel WHEN 'offline' THEN 0 ELSE 1 END,f.mcc,m.id""",
            (brand_id, channel, channel, include_archived),
        ).fetchall()
        support = self._tannei_support_by_merchant(connection, brand_id)
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = row["channel"], row["mcc"]
            group = groups.setdefault(
                key, {"notes": set(), "merchant_ids": [], "evidence_count": 0}
            )
            if row["note"]:
                group["notes"].add(row["note"])
            group["merchant_ids"].append(row["merchant_id"])
            group["evidence_count"] += row["evidence_count"] + support.get(
                (row["merchant_id"], row["mcc"]), 0
            )
        result = []
        for (group_channel, mcc), group in groups.items():
            if len(group["notes"]) > 1:
                raise StoreError(
                    f"У MCC {mcc} одного канала разные примечания; выберите одно вручную"
                )
            result.append(
                BrandMccFact(
                    group_channel,
                    mcc,
                    next(iter(group["notes"]), ""),
                    tuple(group["merchant_ids"]),
                    group["evidence_count"],
                )
            )
        return tuple(result)

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
            """SELECT id,kind,merchant_id,brand_id,actor_id,created_at,reverted_by,changes_json
            FROM store_audit WHERE (? IS NULL OR merchant_id=?)
            ORDER BY id DESC LIMIT ? OFFSET ?""",
            (merchant_id, merchant_id, max(1, min(int(limit), 100)), offset),
        )
        return tuple(self._audit_entry_from_row(connection, row) for row in rows)

    def audit_entry(self, audit_id: int, *, connection=None) -> AuditEntry | None:
        """Return one privacy-safe audit entry, or ``None`` when it does not exist."""

        if isinstance(audit_id, bool) or not isinstance(audit_id, int) or audit_id <= 0:
            raise StoreError("Номер записи истории должен быть положительным целым числом")
        if connection is None:
            with self.connection() as conn:
                return self.audit_entry(audit_id, connection=conn)
        row = connection.execute(
            """SELECT id,kind,merchant_id,brand_id,actor_id,created_at,reverted_by,changes_json
            FROM store_audit WHERE id=?""",
            (audit_id,),
        ).fetchone()
        return self._audit_entry_from_row(connection, row) if row is not None else None

    def brand_history(
        self, brand_id: int, *, limit=20, offset: int = 0, connection=None
    ) -> tuple[AuditEntry, ...]:
        """Aggregate audits for current members and brands merged into a brand."""

        if isinstance(offset, bool) or not isinstance(offset, int):
            raise StoreError("Смещение истории должно быть целым числом")
        offset = max(0, min(offset, 1_000_000))
        if connection is None:
            with self.connection() as conn:
                return self.brand_history(brand_id, limit=limit, offset=offset, connection=conn)
        rows = connection.execute(
            """WITH RECURSIVE related(id) AS (
                SELECT ? UNION ALL
                SELECT b.id FROM store_brands b JOIN related r ON b.merged_into=r.id
            ), members(merchant_id) AS (
                SELECT DISTINCT bm.merchant_id FROM store_brand_members bm
                JOIN related r ON r.id=bm.brand_id
            )
            SELECT DISTINCT a.id,a.kind,a.merchant_id,a.brand_id,a.actor_id,
              a.created_at,a.reverted_by,a.changes_json
            FROM store_audit a
            WHERE a.brand_id IN (SELECT id FROM related)
               OR a.merchant_id IN (SELECT merchant_id FROM members)
            ORDER BY a.id DESC LIMIT ? OFFSET ?""",
            (brand_id, max(1, min(int(limit), 100)), offset),
        )
        return tuple(self._audit_entry_from_row(connection, row) for row in rows)

    @classmethod
    def _audit_entry_from_row(cls, connection: sqlite3.Connection, row: sqlite3.Row) -> AuditEntry:
        values = dict(row)
        changes_json = values.pop("changes_json")
        values["details"] = cls._audit_details(connection, changes_json)
        values["summary"] = cls._audit_summary(connection, values["kind"], changes_json)
        return AuditEntry(**values)

    @staticmethod
    def _audit_summary(connection: sqlite3.Connection, kind: str, changes_json: str) -> str:
        """Return one stable UI label derived from structured audit snapshots."""

        def clipped(value: Any, maximum: int = 48) -> str:
            text = " ".join(str(value or "").split())
            return text if len(text) <= maximum else text[: maximum - 1] + "…"

        def parsed_aliases(raw: str | None) -> list[str]:
            try:
                values = json.loads(raw or "[]")
            except (TypeError, json.JSONDecodeError):
                return []
            return [str(value) for value in values] if isinstance(values, list) else []

        try:
            changes = json.loads(changes_json)
        except (TypeError, json.JSONDecodeError):
            changes = []
        if not isinstance(changes, list):
            changes = []
        edits = [edit for edit in changes if isinstance(edit, dict)]

        merchant_states: dict[int, dict] = {}
        fact_states: dict[int, dict] = {}
        for edit in edits:
            state = edit.get("after") or edit.get("before")
            if not isinstance(state, dict) or not isinstance(edit.get("id"), int):
                continue
            if edit.get("table") == "store_merchants":
                merchant_states[edit["id"]] = state
            elif edit.get("table") == "store_facts":
                fact_states[edit["id"]] = state

        def fact_state(fact_id: Any) -> dict:
            if isinstance(fact_id, int) and fact_id in fact_states:
                return fact_states[fact_id]
            if not isinstance(fact_id, int):
                return {}
            row = connection.execute(
                "SELECT merchant_id,mcc FROM store_facts WHERE id=?", (fact_id,)
            ).fetchone()
            return dict(row) if row is not None else {}

        def channel_for(state: dict) -> str | None:
            merchant_id = state.get("merchant_id")
            merchant = merchant_states.get(merchant_id, {})
            channel = merchant.get("channel")
            if channel not in {"offline", "online"} and isinstance(merchant_id, int):
                row = connection.execute(
                    "SELECT channel FROM store_merchants WHERE id=?", (merchant_id,)
                ).fetchone()
                channel = row["channel"] if row is not None else None
            return channel if channel in {"offline", "online"} else None

        def channel_suffix(channels: set[str | None]) -> str:
            labels = [
                label
                for value, label in (("offline", "офлайн"), ("online", "онлайн"))
                if value in channels
            ]
            return f" · {' и '.join(labels)}" if labels else ""

        # Merge edits also mutate aliases, so identify the structural operation first.
        if kind in {"merge_brand", "merge_merchant"}:
            table = "store_brands" if kind == "merge_brand" else "store_merchants"
            target_id = None
            for edit in edits:
                before, after = edit.get("before"), edit.get("after")
                if (
                    edit.get("table") == table
                    and isinstance(before, dict)
                    and isinstance(after, dict)
                    and before.get("merged_into") != after.get("merged_into")
                    and isinstance(after.get("merged_into"), int)
                ):
                    target_id = after["merged_into"]
                    break
            if target_id is not None:
                for edit in edits:
                    if edit.get("table") != table or edit.get("id") != target_id:
                        continue
                    target = edit.get("after") or edit.get("before")
                    if isinstance(target, dict) and clipped(target.get("name")):
                        return f"объединён с «{clipped(target['name'])}»"
            return "бренды объединены"

        for edit in edits:
            before, after = edit.get("before"), edit.get("after")
            if (
                edit.get("table") in {"store_brands", "store_merchants"}
                and isinstance(before, dict)
                and isinstance(after, dict)
                and before.get("name") != after.get("name")
            ):
                return f"название: «{clipped(before.get('name'))}» → «{clipped(after.get('name'))}»"

        for edit in edits:
            before, after = edit.get("before"), edit.get("after")
            if (
                edit.get("table") not in {"store_brands", "store_merchants"}
                or not isinstance(before, dict)
                or not isinstance(after, dict)
                or before.get("aliases_json") == after.get("aliases_json")
            ):
                continue
            old_aliases = parsed_aliases(before.get("aliases_json"))
            new_aliases = parsed_aliases(after.get("aliases_json"))
            added = [value for value in new_aliases if value not in old_aliases]
            removed = [value for value in old_aliases if value not in new_aliases]
            if len(added) == 1 and not removed:
                return f"добавлено название «{clipped(added[0])}»"
            if len(removed) == 1 and not added:
                return f"удалено название «{clipped(removed[0])}»"
            return "другие названия обновлены"

        added_facts: list[tuple[str, str | None]] = []
        removed_facts: list[tuple[str, str | None]] = []
        note_changes: list[tuple[str, str, str]] = []
        for edit in edits:
            if edit.get("table") != "store_facts":
                continue
            before, after = edit.get("before"), edit.get("after")
            state = after or before
            if not isinstance(state, dict):
                continue
            mcc = clipped(state.get("mcc"), 12)
            channel = channel_for(state)
            if before is None and isinstance(after, dict) and not after.get("archived"):
                added_facts.append((mcc, channel))
            elif isinstance(before, dict) and isinstance(after, dict):
                if not before.get("archived") and after.get("archived"):
                    removed_facts.append((mcc, channel))
                elif before.get("archived") and not after.get("archived"):
                    added_facts.append((mcc, channel))
                if before.get("note", "") != after.get("note", ""):
                    note_changes.append(
                        (
                            mcc,
                            clipped(before.get("note")) or "нет",
                            clipped(after.get("note")) or "нет",
                        )
                    )

        added_facts = list(dict.fromkeys(added_facts))
        removed_facts = list(dict.fromkeys(removed_facts))
        added_mcc = list(dict.fromkeys(mcc for mcc, _channel in added_facts))
        removed_mcc = list(dict.fromkeys(mcc for mcc, _channel in removed_facts))
        if len(added_mcc) == 1 and len(removed_mcc) == 1:
            channels = {
                channel
                for mcc, channel in (*removed_facts, *added_facts)
                if mcc in {removed_mcc[0], added_mcc[0]}
            }
            return f"MCC {removed_mcc[0]} → {added_mcc[0]}{channel_suffix(channels)}"
        if added_facts:
            channels = {channel for _mcc, channel in added_facts}
            label = ", ".join(added_mcc[:3])
            if len(added_mcc) > 3:
                label += f" и ещё {len(added_mcc) - 3}"
            prefix = "добавлен MCC" if len(added_mcc) == 1 else "добавлены MCC"
            return f"{prefix} {label}{channel_suffix(channels)}"
        if removed_facts:
            channels = {channel for _mcc, channel in removed_facts}
            label = ", ".join(removed_mcc[:3])
            if len(removed_mcc) > 3:
                label += f" и ещё {len(removed_mcc) - 3}"
            prefix = "удалён MCC" if len(removed_mcc) == 1 else "удалены MCC"
            return f"{prefix} {label}{channel_suffix(channels)}"
        if note_changes:
            mcc, old, new = note_changes[0]
            return f"примечание MCC {mcc}: «{old}» → «{new}»"

        evidence_added: list[tuple[str, str | None]] = []
        evidence_revoked: list[tuple[str, str | None]] = []
        for edit in edits:
            if edit.get("table") != "store_evidence":
                continue
            before, after = edit.get("before"), edit.get("after")
            state = after or before
            if not isinstance(state, dict):
                continue
            fact = fact_state(state.get("fact_id"))
            value = (clipped(fact.get("mcc"), 12), channel_for(fact))
            if before is None and isinstance(after, dict) and not after.get("revoked"):
                evidence_added.append(value)
            elif (
                isinstance(before, dict)
                and isinstance(after, dict)
                and not before.get("revoked")
                and after.get("revoked")
            ):
                evidence_revoked.append(value)
        evidence_added = [value for value in dict.fromkeys(evidence_added) if value[0]]
        evidence_revoked = [value for value in dict.fromkeys(evidence_revoked) if value[0]]
        if evidence_added:
            mcc_values = list(dict.fromkeys(mcc for mcc, _channel in evidence_added))
            channels = {channel for _mcc, channel in evidence_added}
            label = ", ".join(mcc_values[:3])
            prefix = "подтверждён MCC" if len(mcc_values) == 1 else "подтверждены MCC"
            return f"{prefix} {label}{channel_suffix(channels)}"
        if evidence_revoked:
            mcc_values = list(dict.fromkeys(mcc for mcc, _channel in evidence_revoked))
            channels = {channel for _mcc, channel in evidence_revoked}
            label = ", ".join(mcc_values[:3])
            return f"подтверждение MCC {label} отменено{channel_suffix(channels)}"

        for edit in edits:
            before, after = edit.get("before"), edit.get("after")
            if (
                edit.get("table") == "store_merchants"
                and before is None
                and isinstance(after, dict)
            ):
                return f"добавлен магазин{channel_suffix({after.get('channel')})}"
            if (
                edit.get("table") == "store_merchants"
                and isinstance(before, dict)
                and isinstance(after, dict)
                and not before.get("archived")
                and after.get("archived")
            ):
                return f"удалён магазин{channel_suffix({after.get('channel')})}"
            if (
                edit.get("table") == "store_brand_members"
                and isinstance(before, dict)
                and isinstance(after, dict)
                and before.get("brand_id") != after.get("brand_id")
            ):
                return "изменена группа бренда"
        if any(edit.get("table") == "store_tannei_snapshots" for edit in edits):
            return "обновлены данные MCC"
        return "обновлена запись"

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
        note_changes: list[str] = []
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
            elif table == "store_brands":
                if before is None and after:
                    details.append(f"Добавлен бренд «{clipped(after.get('name', ''))}».")
                    continue
                if not before or not after:
                    continue
                if before.get("name") != after.get("name"):
                    details.append(
                        f"Название бренда: «{clipped(before.get('name', ''))}» → "
                        f"«{clipped(after.get('name', ''))}»."
                    )
                if before.get("aliases_json") != after.get("aliases_json"):
                    details.append(
                        f"Другие названия бренда: {aliases(before.get('aliases_json'))} → "
                        f"{aliases(after.get('aliases_json'))}."
                    )
                if before.get("merged_into") != after.get("merged_into") and after.get(
                    "merged_into"
                ):
                    details.append("Бренды объединены.")
            elif table == "store_brand_members":
                if before and after and before.get("brand_id") != after.get("brand_id"):
                    details.append("Магазин перенесён в другую группу бренда.")
            elif table == "store_facts":
                state = after or before or {}
                mcc = str(state.get("mcc", ""))
                if before is None and after:
                    added_mcc.append(mcc)
                elif before and after and before.get("archived") != after.get("archived"):
                    (hidden_mcc if after.get("archived") else restored_mcc).append(mcc)
                if before and after and before.get("note", "") != after.get("note", ""):
                    old = clipped(before.get("note", ""), 48) or "нет"
                    new = clipped(after.get("note", ""), 48) or "нет"
                    note_changes.append(f"Примечание MCC {mcc}: «{old}» → «{new}».")
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
            elif table == "store_tannei_snapshots":
                before_public = (
                    StoreRepository._public_snapshot(before) if before is not None else None
                )
                after_public = (
                    StoreRepository._public_snapshot(after) if after is not None else None
                )
                for channel in ("offline", "online"):
                    old_values = (
                        before_public["channels"][channel] if before_public is not None else {}
                    )
                    new_values = (
                        after_public["channels"][channel] if after_public is not None else {}
                    )
                    for mcc in set(old_values) | set(new_values):
                        old_count = old_values.get(mcc, {}).get("support_count", 0)
                        new_count = new_values.get(mcc, {}).get("support_count", 0)
                        delta = new_count - old_count
                        if delta > 0:
                            key = "tannei.by", mcc
                            evidence_added[key] = evidence_added.get(key, 0) + delta
                        elif delta < 0:
                            key = "tannei.by", mcc
                            evidence_revoked[key] = evidence_revoked.get(key, 0) - delta
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
        details.extend(note_changes[:5])
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
    def _required_brand(connection, brand_id):
        row = connection.execute("SELECT * FROM store_brands WHERE id=?", (brand_id,)).fetchone()
        if row is None or row["archived"]:
            raise StoreError("Бренд недоступен; откройте поиск заново")
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

    def _create_brand(self, connection, changes, *, name, aliases=()):
        return self._insert(
            connection,
            changes,
            "store_brands",
            name=_name(name),
            aliases_json=_json(_aliases(aliases)),
        )

    def _attach_brand(self, connection, changes, merchant_id, brand_id):
        self._required_brand(connection, brand_id)
        row = connection.execute(
            "SELECT * FROM store_brand_members WHERE merchant_id=?", (merchant_id,)
        ).fetchone()
        if row is None:
            self._insert(
                connection,
                changes,
                "store_brand_members",
                brand_id=brand_id,
                merchant_id=merchant_id,
            )
        elif row["brand_id"] != brand_id:
            self._update(connection, changes, "store_brand_members", row["id"], brand_id=brand_id)
        self._harmonize_brand_notes(connection, changes, brand_id)

    def _harmonize_brand_notes(self, connection, changes, brand_id):
        """Apply the empty/identical/conflict policy within channel MCC groups."""

        rows = connection.execute(
            """SELECT f.*,m.channel FROM store_brand_members bm
            JOIN store_merchants m ON m.id=bm.merchant_id
            JOIN store_facts f ON f.merchant_id=m.id
            WHERE bm.brand_id=? ORDER BY m.channel,f.mcc,f.id""",
            (brand_id,),
        ).fetchall()
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault((row["channel"], row["mcc"]), []).append(row)
        for (channel, mcc), facts in groups.items():
            notes = {fact["note"] for fact in facts if fact["note"]}
            if len(notes) > 1:
                label = "онлайн" if channel == "online" else "обычного магазина"
                raise StoreError(
                    f"У MCC {mcc} для {label} разные примечания; выберите одно вручную"
                )
            if not notes:
                continue
            note = next(iter(notes))
            for fact in facts:
                if not fact["note"]:
                    self._update(
                        connection,
                        changes,
                        "store_facts",
                        fact["id"],
                        note=note,
                        revision=fact["revision"] + 1,
                    )

    def _fact(self, connection, changes, merchant_id, mcc, *, reactivate=True, note=None):
        mcc = normalize_mcc(mcc)
        incoming_note = _note(note) if note is not None else None
        row = connection.execute(
            "SELECT * FROM store_facts WHERE merchant_id=? AND mcc=?", (merchant_id, mcc)
        ).fetchone()
        if row is None:
            return self._insert(
                connection,
                changes,
                "store_facts",
                merchant_id=merchant_id,
                mcc=mcc,
                note=incoming_note or "",
            )
        if incoming_note and row["note"] and incoming_note != row["note"]:
            raise StoreError("У MCC уже другое примечание; измените его отдельно")
        values = {}
        if incoming_note and not row["note"]:
            values["note"] = incoming_note
        if row["archived"] and reactivate:
            values["archived"] = 0
        if values:
            self._update(
                connection,
                changes,
                "store_facts",
                row["id"],
                **values,
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
            "image_sha256",
            "row_number",
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

    def confirm_mcc(
        self,
        merchant_id: int,
        mcc: str,
        *,
        actor_id: int,
        source: str,
        source_key: str,
        evidence: dict | None = None,
        note: str | None = None,
        connection=None,
    ) -> ChangeResult:
        """Idempotently add deterministic evidence, optionally in a caller transaction.

        A curated package should derive ``source_key`` from immutable input,
        for example ``<image SHA256>:<row number>``. Reusing that key for the
        same fact is a no-op; reusing it for a different fact is rejected.
        """

        if connection is None:
            with self.transaction() as conn:
                return self.confirm_mcc(
                    merchant_id,
                    mcc,
                    actor_id=actor_id,
                    source=source,
                    source_key=source_key,
                    evidence=evidence,
                    note=note,
                    connection=conn,
                )
        if (
            not isinstance(source, str)
            or not source.strip()
            or len(source.strip()) > 50
            or not isinstance(source_key, str)
            or not source_key.strip()
            or len(source_key) > 240
        ):
            raise StoreError("Источник подтверждения или его ключ недопустим")
        connection.execute("SAVEPOINT store_confirmation")
        try:
            self._required(connection, int(merchant_id))
            changes = _Changes(connection)
            fact_id = self._fact(connection, changes, int(merchant_id), mcc, note=note)
            self._evidence(
                connection,
                changes,
                fact_id,
                evidence,
                source=source.strip(),
                key=source_key,
            )
            edits = changes.finish()
            brand_id = self._brand_id_for_merchant(connection, int(merchant_id))
            if not edits:
                result = ChangeResult(0, int(merchant_id), brand_id, changed=False)
            else:
                cursor = connection.execute(
                    """INSERT INTO store_audit
                    (kind,merchant_id,brand_id,actor_id,changes_json)
                    VALUES('confirm_mcc',?,?,?,?)""",
                    (int(merchant_id), brand_id, int(actor_id), _json(edits)),
                )
                result = ChangeResult(cursor.lastrowid, int(merchant_id), brand_id)
        except BaseException:
            connection.execute("ROLLBACK TO store_confirmation")
            connection.execute("RELEASE store_confirmation")
            raise
        connection.execute("RELEASE store_confirmation")
        return result

    def apply_change(
        self, kind: str, payload: dict, actor_id: int, *, connection=None
    ) -> ChangeResult:
        """Validate and atomically apply one audited edit; accept a review transaction.

        Supported kinds: add_merchant, add_mcc, add_mcc_both, replace_mcc, archive_mcc,
        edit_mcc_note, rename_merchant, aliases, archive_merchant,
        rename_brand, brand_aliases, edit_brand_names, set_brand_membership, merge_brand,
        merge_merchant, revert.
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
        brand_id = None
        if kind == "revert":
            merchant_id, reverted = self._revert(connection, changes, payload["audit_id"])
            brand_id = self._brand_id_for_merchant(connection, merchant_id)
        elif kind == "add_merchant":
            merchant_id = self._insert(
                connection,
                changes,
                "store_merchants",
                name=_name(payload["name"]),
                channel=_channel(payload.get("channel", "offline")),
                aliases_json=_json(_aliases(payload.get("aliases", []))),
            )
            if payload.get("brand_id") is None:
                brand_id = self._create_brand(
                    connection,
                    changes,
                    name=payload["name"],
                    aliases=payload.get("aliases", []),
                )
            else:
                brand_id = int(payload["brand_id"])
            self._attach_brand(connection, changes, merchant_id, brand_id)
            if payload.get("mcc"):
                fact_id = self._fact(
                    connection,
                    changes,
                    merchant_id,
                    payload["mcc"],
                    note=payload.get("note"),
                )
                changes.touch("store_facts", fact_id)
                self._evidence(connection, changes, fact_id, payload.get("evidence"))
                self._harmonize_brand_notes(connection, changes, brand_id)
        elif kind == "add_mcc_both":
            merchant_id, brand_id = self._add_mcc_both(connection, changes, payload)
        elif kind in {
            "rename_brand",
            "brand_aliases",
            "edit_brand_names",
            "set_brand_membership",
            "merge_brand",
        }:
            if kind == "set_brand_membership":
                merchant_id = int(payload["merchant_id"])
                self._required(connection, merchant_id)
                brand_id = int(payload["brand_id"])
                self._attach_brand(connection, changes, merchant_id, brand_id)
            else:
                brand_id = int(payload["brand_id"])
                brand = self._required_brand(connection, brand_id)
                if kind == "rename_brand":
                    self._update(
                        connection,
                        changes,
                        "store_brands",
                        brand_id,
                        name=_name(payload["name"]),
                        revision=brand["revision"] + 1,
                    )
                elif kind == "edit_brand_names":
                    self._update(
                        connection,
                        changes,
                        "store_brands",
                        brand_id,
                        name=_name(payload["name"]),
                        aliases_json=_json(_aliases(payload["aliases"])),
                        revision=brand["revision"] + 1,
                    )
                elif kind == "brand_aliases":
                    self._update(
                        connection,
                        changes,
                        "store_brands",
                        brand_id,
                        aliases_json=_json(_aliases(payload["aliases"])),
                        revision=brand["revision"] + 1,
                    )
                else:
                    brand_id = self._merge_brands(
                        connection, changes, brand, int(payload["target_id"])
                    )
            merchant_id = self._brand_anchor(connection, brand_id)
        else:
            merchant_id = int(payload["merchant_id"])
            merchant = self._required(connection, merchant_id)
            brand_id = self._brand_id_for_merchant(connection, merchant_id)
            fact_merchant_ids = [merchant_id]
            if kind in {"replace_mcc", "archive_mcc", "edit_mcc_note"}:
                fact_merchant_ids = self._fact_group_merchants(
                    connection,
                    payload,
                    merchant,
                    brand_id,
                    payload.get("old_mcc", payload.get("mcc")),
                )
            if kind in {"add_mcc", "replace_mcc"}:
                if kind == "replace_mcc":
                    if normalize_mcc(payload["old_mcc"]) == normalize_mcc(payload["mcc"]):
                        raise StoreError("Новый MCC совпадает с прежним")
                    for target_id in fact_merchant_ids:
                        self._archive_fact(connection, changes, target_id, payload["old_mcc"])
                for target_id in fact_merchant_ids:
                    fact_id = self._fact(
                        connection,
                        changes,
                        target_id,
                        payload["mcc"],
                        note=payload.get("note"),
                    )
                    evidence = payload.get("evidence")
                    evidence_key = None
                    if (
                        len(fact_merchant_ids) > 1
                        and isinstance(evidence, dict)
                        and evidence.get("submission_id") is not None
                    ):
                        evidence_key = f"{evidence['submission_id']}:{target_id}"
                    self._evidence(connection, changes, fact_id, evidence, key=evidence_key)
                    changes.touch("store_facts", fact_id)
            elif kind == "archive_mcc":
                for target_id in fact_merchant_ids:
                    self._archive_fact(connection, changes, target_id, payload["mcc"])
            elif kind == "edit_mcc_note":
                self._edit_mcc_note(
                    connection, changes, merchant_id, payload["mcc"], payload["note"]
                )
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
                merchant_id, brand_id = self._merge(
                    connection, changes, merchant, int(payload["target_id"])
                )
            else:
                raise StoreError("Неизвестное изменение")
        edits = changes.finish()
        cursor = connection.execute(
            """INSERT INTO store_audit
            (kind,merchant_id,brand_id,actor_id,changes_json) VALUES(?,?,?,?,?)""",
            (kind, merchant_id, brand_id, actor_id, _json(edits)),
        )
        if kind == "merge_merchant":
            self._record_merge_guard(connection, cursor.lastrowid, edits)
        if reverted is not None:
            connection.execute(
                "UPDATE store_audit SET reverted_by=? WHERE id=?", (cursor.lastrowid, reverted)
            )
        return ChangeResult(cursor.lastrowid, merchant_id, brand_id)

    def _add_mcc_both(self, connection, changes, payload):
        """Add one MCC to real offline and online branches in a single audit."""

        has_brand = payload.get("brand_id") is not None
        has_name = payload.get("name") is not None
        if has_brand == has_name:
            raise StoreError("Укажите существующий бренд или название нового бренда")
        if has_brand:
            brand_id = int(payload["brand_id"])
            brand = self._required_brand(connection, brand_id)
            aliases: list[str] = []
        else:
            name = _name(payload["name"])
            aliases = _aliases(payload.get("aliases", []))
            brand_id = self._create_brand(connection, changes, name=name, aliases=aliases)
            brand = connection.execute(
                "SELECT * FROM store_brands WHERE id=?", (brand_id,)
            ).fetchone()
        mcc = normalize_mcc(payload["mcc"])
        targets: list[int] = []
        for channel in ("offline", "online"):
            members = connection.execute(
                """SELECT m.* FROM store_brand_members bm
                JOIN store_merchants m ON m.id=bm.merchant_id
                WHERE bm.brand_id=? AND m.channel=? AND m.archived=0 ORDER BY m.id""",
                (brand_id, channel),
            ).fetchall()
            if not members:
                merchant_id = self._insert(
                    connection,
                    changes,
                    "store_merchants",
                    name=brand["name"],
                    channel=channel,
                    aliases_json=_json(aliases),
                )
                self._attach_brand(connection, changes, merchant_id, brand_id)
            else:
                existing = connection.execute(
                    """SELECT m.id FROM store_brand_members bm
                    JOIN store_merchants m ON m.id=bm.merchant_id
                    JOIN store_facts f ON f.merchant_id=m.id
                    WHERE bm.brand_id=? AND m.channel=? AND m.archived=0 AND f.mcc=?
                    ORDER BY f.archived,m.id LIMIT 1""",
                    (brand_id, channel, mcc),
                ).fetchone()
                merchant_id = existing["id"] if existing is not None else members[0]["id"]
            existing_fact = connection.execute(
                "SELECT 1 FROM store_facts WHERE merchant_id=? AND mcc=?",
                (merchant_id, mcc),
            ).fetchone()
            fact_id = self._fact(
                connection,
                changes,
                merchant_id,
                mcc,
                note=None if existing_fact is not None else payload.get("note"),
            )
            evidence = payload.get("evidence")
            key = None
            if isinstance(evidence, dict) and evidence.get("submission_id") is not None:
                key = f"{evidence['submission_id']}:{channel}:{merchant_id}"
            self._evidence(connection, changes, fact_id, evidence, key=key)
            changes.touch("store_facts", fact_id)
            targets.append(merchant_id)
        self._harmonize_brand_notes(connection, changes, brand_id)
        return targets[0], brand_id

    def _fact_group_merchants(self, connection, payload, merchant, brand_id, mcc):
        """Validate all internal rows represented by one public channel/MCC fact."""

        values = payload.get("merchant_ids")
        if values is None:
            return [merchant["id"]]
        if (
            not isinstance(values, list)
            or not values
            or len(values) > 100
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in values
            )
            or len(set(values)) != len(values)
            or values[0] != merchant["id"]
        ):
            raise StoreError("Некорректная группа MCC")
        normalized_mcc = normalize_mcc(mcc)
        current_ids = [
            row["merchant_id"]
            for row in connection.execute(
                """SELECT f.merchant_id FROM store_brand_members bm
                JOIN store_merchants m ON m.id=bm.merchant_id
                JOIN store_facts f ON f.merchant_id=m.id
                WHERE bm.brand_id=? AND m.channel=? AND m.archived=0
                  AND f.mcc=? AND f.archived=0 ORDER BY f.merchant_id""",
                (brand_id, merchant["channel"], normalized_mcc),
            ).fetchall()
        ]
        if current_ids != values:
            raise StoreError("Группа MCC уже изменилась; откройте её заново")
        for value in values:
            target = self._required(connection, value)
            if (
                target["channel"] != merchant["channel"]
                or self._brand_id_for_merchant(connection, value) != brand_id
                or connection.execute(
                    "SELECT 1 FROM store_facts WHERE merchant_id=? AND mcc=? AND archived=0",
                    (value, normalized_mcc),
                ).fetchone()
                is None
            ):
                raise StoreError("Группа MCC уже изменилась; откройте её заново")
        return values

    @staticmethod
    def _brand_id_for_merchant(connection, merchant_id):
        row = connection.execute(
            "SELECT brand_id FROM store_brand_members WHERE merchant_id=?", (merchant_id,)
        ).fetchone()
        if row is None:
            raise StoreError("Для магазина не найдена группа бренда")
        return row["brand_id"]

    @staticmethod
    def _brand_anchor(connection, brand_id):
        row = connection.execute(
            "SELECT min(merchant_id) AS merchant_id FROM store_brand_members WHERE brand_id=?",
            (brand_id,),
        ).fetchone()
        return row["merchant_id"] or 0

    def _edit_mcc_note(self, connection, changes, merchant_id, mcc, note):
        mcc = normalize_mcc(mcc)
        note = _note(note)
        merchant = self._required(connection, merchant_id)
        fact = connection.execute(
            """SELECT * FROM store_facts
            WHERE merchant_id=? AND mcc=? AND archived=0""",
            (merchant_id, mcc),
        ).fetchone()
        if fact is None:
            raise StoreError("Такого действующего MCC у магазина нет")
        brand_id = self._brand_id_for_merchant(connection, merchant_id)
        siblings = connection.execute(
            """SELECT f.* FROM store_brand_members bm
            JOIN store_merchants m ON m.id=bm.merchant_id
            JOIN store_facts f ON f.merchant_id=m.id
            WHERE bm.brand_id=? AND m.channel=? AND f.mcc=?""",
            (brand_id, merchant["channel"], mcc),
        ).fetchall()
        for sibling in siblings:
            if sibling["note"] != note:
                self._update(
                    connection,
                    changes,
                    "store_facts",
                    sibling["id"],
                    note=note,
                    revision=sibling["revision"] + 1,
                )

    def _merge_brands(self, connection, changes, source, target_id):
        target = self._required_brand(connection, target_id)
        if source["id"] == target_id:
            raise StoreError("Нельзя объединить бренд с самим собой")
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
            "store_brands",
            target_id,
            aliases_json=_json(aliases),
            revision=target["revision"] + 1,
        )
        for member in connection.execute(
            "SELECT * FROM store_brand_members WHERE brand_id=? ORDER BY id", (source["id"],)
        ).fetchall():
            self._update(
                connection,
                changes,
                "store_brand_members",
                member["id"],
                brand_id=target_id,
            )
        self._harmonize_brand_notes(connection, changes, target_id)
        self._merge_tannei_snapshots(connection, changes, source["id"], target_id)
        self._update(
            connection,
            changes,
            "store_brands",
            source["id"],
            archived=1,
            merged_into=target_id,
            revision=source["revision"] + 1,
        )
        return target_id

    def _merge_tannei_snapshots(self, connection, changes, source_id, target_id):
        """Union source support into the surviving brand while retaining an undo source."""

        source = connection.execute(
            "SELECT * FROM store_tannei_snapshots WHERE brand_id=?", (source_id,)
        ).fetchone()
        if source is None:
            return
        target = connection.execute(
            "SELECT * FROM store_tannei_snapshots WHERE brand_id=?", (target_id,)
        ).fetchone()
        payload = self._merge_snapshot_payloads(
            self._snapshot_payload(target), self._snapshot_payload(source)
        )
        encoded = _json(payload)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        if target is None:
            self._insert(
                connection,
                changes,
                "store_tannei_snapshots",
                brand_id=target_id,
                revision=1,
                updated_at=now,
                snapshot_json=encoded,
            )
        elif target["snapshot_json"] != encoded:
            self._update(
                connection,
                changes,
                "store_tannei_snapshots",
                target["id"],
                revision=target["revision"] + 1,
                updated_at=now,
                snapshot_json=encoded,
            )

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
        source_brand_id = self._brand_id_for_merchant(connection, source["id"])
        target_brand_id = self._brand_id_for_merchant(connection, target_id)
        if source_brand_id != target_brand_id:
            source_brand = self._required_brand(connection, source_brand_id)
            target_brand_id = self._merge_brands(connection, changes, source_brand, target_brand_id)
        else:
            self._harmonize_brand_notes(connection, changes, target_brand_id)
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
            target_fact = self._fact(
                connection,
                changes,
                target_id,
                fact["mcc"],
                reactivate=False,
                note=fact["note"] or None,
            )
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
        return source["id"], target_brand_id

    def _revert(self, connection, changes, audit_id):
        audit = connection.execute("SELECT * FROM store_audit WHERE id=?", (audit_id,)).fetchone()
        if audit is None or audit["reverted_by"] is not None or audit["kind"] == "revert":
            raise StaleChangeError("Изменение уже отменено или недоступно для отмены")
        edits = json.loads(audit["changes_json"])
        if audit["kind"] == "merge_merchant":
            if self._merge_guard_is_stale(connection, audit["id"]):
                raise StaleChangeError(
                    "После объединения появились импортированные данные; "
                    "внесите отдельное исправление"
                )
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
            if self._merge_has_new_tannei_sources(connection, edits):
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
        order = {
            "store_evidence": 0,
            "store_sources": 1,
            "store_tannei_snapshots": 2,
            "store_facts": 3,
            "store_brand_members": 4,
            "store_merchants": 5,
            "store_brands": 6,
        }
        for edit in sorted(edits, key=lambda item: order[item["table"]]):
            table, row_id, before, after = edit["table"], edit["id"], edit["before"], edit["after"]
            if table == "store_evidence" and before is None:
                self._update(connection, changes, table, row_id, revoked=1)
            elif table == "store_tannei_snapshots" and audit["kind"] == "import":
                self._revoke_import_snapshot_additions(connection, changes, row_id, before, after)
            elif table == "store_tannei_snapshots" and before is None:
                changes.touch(table, row_id)
                connection.execute("DELETE FROM store_tannei_snapshots WHERE id=?", (row_id,))
            elif table == "store_facts":
                supported = connection.execute(
                    """SELECT 1 FROM store_evidence
                    WHERE fact_id=? AND revoked=0 AND source<>'tannei'""",
                    (row_id,),
                ).fetchone()
                if not supported:
                    fact_state = after or before
                    merchant_id = fact_state["merchant_id"]
                    brand_id = self._brand_id_for_merchant(connection, merchant_id)
                    merchant = connection.execute(
                        "SELECT channel FROM store_merchants WHERE id=?", (merchant_id,)
                    ).fetchone()
                    if merchant is None:
                        raise StaleChangeError("Магазин больше недоступен")
                    supported = bool(
                        self.brand_mcc_has_tannei(
                            brand_id,
                            merchant["channel"],
                            fact_state["mcc"],
                            connection=connection,
                        )
                    )
                archived = before["archived"] if before else 1
                if not supported:
                    archived = 1
                if archived and not after["archived"] and supported:
                    continue
                values = {
                    "archived": archived,
                    "revision": after["revision"] + 1,
                }
                if before is not None and before["note"] != after["note"]:
                    values["note"] = before["note"]
                self._update(
                    connection,
                    changes,
                    table,
                    row_id,
                    **values,
                )
            elif table == "store_merchants" and before is None:
                if connection.execute(
                    "SELECT 1 FROM store_facts WHERE merchant_id=? AND archived=0", (row_id,)
                ).fetchone():
                    raise StaleChangeError("У магазина уже есть независимые подтверждения")
                self._update(
                    connection, changes, table, row_id, archived=1, revision=after["revision"] + 1
                )
            elif table == "store_brands" and before is None:
                self._update(
                    connection,
                    changes,
                    table,
                    row_id,
                    archived=1,
                    revision=after["revision"] + 1,
                )
            elif table == "store_brand_members" and before is None:
                # Membership identity remains as the tombstone for an archived
                # merchant/brand pair, just like imported source identities.
                continue
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
        if audit["kind"] in {"merge_merchant", "merge_brand"}:
            brand_ids = {audit["brand_id"]} if audit["brand_id"] is not None else set()
            for edit in edits:
                if edit["table"] == "store_brand_members":
                    for state in (edit.get("before"), edit.get("after")):
                        if state is not None:
                            brand_ids.add(state["brand_id"])
            self._redistribute_tannei_snapshots(connection, changes, brand_ids)
        return audit["merchant_id"], audit["id"]

    def _revoke_import_snapshot_additions(self, connection, changes, row_id, before, after):
        """Keep reverted source keys as tombstones so reimport cannot reactivate them."""

        baseline = self._snapshot_payload(before)
        current = self._snapshot_payload(after)
        stores = {
            store_id: {
                "channel": source["channel"],
                "observations": dict(source["observations"]),
            }
            for store_id, source in baseline["stores"].items()
        }
        for store_id, source in current["stores"].items():
            target = stores.setdefault(store_id, {"channel": source["channel"], "observations": {}})
            for key, observation in source["observations"].items():
                if key not in target["observations"]:
                    target["observations"][key] = {**observation, "revoked": True}
        encoded = _json(self._merge_snapshot_payloads({"stores": stores}))
        row = connection.execute(
            "SELECT * FROM store_tannei_snapshots WHERE id=?", (row_id,)
        ).fetchone()
        if row is None:
            raise StaleChangeError("Снимок tannei.by уже изменился")
        self._update(
            connection,
            changes,
            "store_tannei_snapshots",
            row_id,
            revision=row["revision"] + 1,
            updated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            snapshot_json=encoded,
        )

    @staticmethod
    def _merge_has_new_tannei_sources(connection, edits):
        expected = {
            edit["id"]
            for edit in edits
            if edit.get("table") == "store_sources"
            and (edit.get("after") or edit.get("before") or {}).get("source") == "tannei"
        }
        identities = {
            state["source_identity"]
            for edit in edits
            if edit.get("table") == "store_merchants"
            for state in (edit.get("before"),)
            if state and state.get("source_identity")
        }
        for identity in identities:
            if identity.startswith("tannei:network:"):
                network_id = identity.rsplit(":", 1)[-1]
                rows = connection.execute(
                    "SELECT id FROM store_sources WHERE source='tannei' AND network_id=?",
                    (network_id,),
                )
            else:
                store_id = identity.rsplit(":", 1)[-1]
                rows = connection.execute(
                    "SELECT id FROM store_sources WHERE source='tannei' AND store_id=?",
                    (store_id,),
                )
            if any(row["id"] not in expected for row in rows):
                return True
        return False

    def _redistribute_tannei_snapshots(self, connection, changes, brand_ids):
        """Repartition source ledgers after a brand/merchant merge is reverted."""

        brand_ids = {int(value) for value in brand_ids if value is not None}
        if not brand_ids:
            return
        combined = {"stores": {}}
        for row in connection.execute(
            f"SELECT * FROM store_tannei_snapshots WHERE brand_id IN "
            f"({','.join('?' for _ in brand_ids)})",
            tuple(sorted(brand_ids)),
        ).fetchall():
            combined = self._merge_snapshot_payloads(combined, self._snapshot_payload(row))
        desired = {brand_id: {"stores": {}} for brand_id in brand_ids}
        for store_id, source in combined["stores"].items():
            link = connection.execute(
                """SELECT bm.brand_id FROM store_sources s
                JOIN store_brand_members bm ON bm.merchant_id=s.merchant_id
                WHERE s.source='tannei' AND s.store_id=?""",
                (store_id,),
            ).fetchone()
            if link is not None and link["brand_id"] in desired:
                desired[link["brand_id"]]["stores"][store_id] = source
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for brand_id, payload in desired.items():
            row = connection.execute(
                "SELECT * FROM store_tannei_snapshots WHERE brand_id=?", (brand_id,)
            ).fetchone()
            encoded = _json(payload)
            if not payload["stores"]:
                if row is not None:
                    changes.touch("store_tannei_snapshots", row["id"])
                    connection.execute(
                        "DELETE FROM store_tannei_snapshots WHERE id=?", (row["id"],)
                    )
            elif row is None:
                self._insert(
                    connection,
                    changes,
                    "store_tannei_snapshots",
                    brand_id=brand_id,
                    revision=1,
                    updated_at=now,
                    snapshot_json=encoded,
                )
            elif row["snapshot_json"] != encoded:
                self._update(
                    connection,
                    changes,
                    "store_tannei_snapshots",
                    row["id"],
                    revision=row["revision"] + 1,
                    updated_at=now,
                    snapshot_json=encoded,
                )

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
        """Publish one validated Tannei source record through the batch transaction."""

        return self.import_stores(((metadata, observations),))[0]

    def import_stores(
        self,
        records: list[tuple[dict, list[dict]]] | tuple[tuple[dict, list[dict]], ...],
        *,
        checkpoint_done: bool = False,
    ) -> tuple[ChangeResult, ...]:
        """Publish a staged batch atomically without accumulating automated audits."""

        records = list(records)
        if not records:
            return ()
        with self.transaction() as connection:
            changes = _Changes(connection)
            identities = []
            for metadata, observations in records:
                touched_before = set(changes.before)
                merchant_id, brand_id, source_id, guard_changed = self._stage_import_store(
                    connection, changes, metadata, observations
                )
                identities.append(
                    (
                        merchant_id,
                        brand_id,
                        source_id,
                        guard_changed or set(changes.before) != touched_before,
                    )
                )
            results = tuple(
                ChangeResult(0, merchant_id, brand_id, changed=changed)
                for merchant_id, brand_id, _source_id, changed in identities
            )
            if checkpoint_done:
                for result, (_merchant_id, _brand_id, source_id, _changed) in zip(
                    results, identities, strict=True
                ):
                    self._write_checkpoint(
                        connection, f"done:{source_id}", {"merchant_id": result.merchant_id}
                    )
            return results

    @staticmethod
    def _write_checkpoint(connection, key, value):
        connection.execute(
            """INSERT INTO store_import_checkpoints(key,value_json) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
            updated_at=CURRENT_TIMESTAMP""",
            (key, _json(value)),
        )

    def _stage_import_store(self, connection, changes, metadata, observations):
        """Merge one source response into a transaction-local canonical snapshot."""

        source_id, network_id = str(metadata["id"]), metadata.get("network_id")
        channel = "online" if metadata["is_online"] else "offline"
        identity = self._tannei_identity(metadata)
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
                channel=channel,
                source_identity=identity,
            )
            brand_id = self._create_brand(connection, changes, name=name)
            self._attach_brand(connection, changes, merchant_id, brand_id)
        else:
            brand_id = self._brand_id_for_merchant(connection, merchant_id)
        encoded_metadata = _json(metadata)
        if link is None:
            source_link_id = self._insert(
                connection,
                changes,
                "store_sources",
                source="tannei",
                store_id=source_id,
                merchant_id=merchant_id,
                network_id=str(network_id) if network_id else None,
                metadata_json=encoded_metadata,
            )
        else:
            source_link_id = link["id"]
            if link["metadata_json"] != encoded_metadata:
                self._update(
                    connection,
                    changes,
                    "store_sources",
                    link["id"],
                    metadata_json=encoded_metadata,
                )

        snapshot = connection.execute(
            "SELECT * FROM store_tannei_snapshots WHERE brand_id=?", (brand_id,)
        ).fetchone()
        payload = self._snapshot_payload(snapshot)
        stores = dict(payload["stores"])
        current_source = stores.get(source_id, {"channel": channel, "observations": {}})
        if current_source["channel"] != channel:
            raise StoreError("Source store changed channel; explicit review is required")
        source_observations = dict(current_source["observations"])
        occurrences: dict[str, int] = {}
        for observation in observations:
            digest = hashlib.sha256(_json(observation).encode()).hexdigest()
            occurrence = occurrences.get(digest, 0)
            occurrences[digest] = occurrence + 1
            mcc = normalize_mcc(observation["mcc"])
            self._fact(connection, changes, merchant_id, mcc, reactivate=False)
            key = f"{source_id}:{digest}:{occurrence}"
            value = {item: value for item, value in observation.items() if item != "mcc"}
            value["mcc"] = mcc
            existing = source_observations.get(key)
            if existing is not None and existing != value and not existing.get("revoked"):
                raise StoreError("Source observation changed; explicit review is required")
            if existing is None:
                source_observations[key] = value
        stores[source_id] = {"channel": channel, "observations": source_observations}
        updated_payload = self._merge_snapshot_payloads({"stores": stores})
        encoded_snapshot = _json(updated_payload)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        if snapshot is None:
            self._insert(
                connection,
                changes,
                "store_tannei_snapshots",
                brand_id=brand_id,
                revision=1,
                updated_at=now,
                snapshot_json=encoded_snapshot,
            )
        elif snapshot["snapshot_json"] != encoded_snapshot:
            self._update(
                connection,
                changes,
                "store_tannei_snapshots",
                snapshot["id"],
                revision=snapshot["revision"] + 1,
                updated_at=now,
                snapshot_json=encoded_snapshot,
            )
        guard_changed = self._update_import_guard(
            connection,
            source_link_id,
            identity,
            encoded_metadata,
            _json(stores[source_id]),
        )
        return merchant_id, brand_id, source_id, guard_changed

    @staticmethod
    def _update_import_guard(
        connection, store_source_id, source_identity, metadata_json, source_snapshot_json
    ):
        fingerprint = hashlib.sha256(
            f"{metadata_json}\n{source_snapshot_json}".encode()
        ).hexdigest()
        row = connection.execute(
            "SELECT * FROM store_tannei_import_guards WHERE store_source_id=?",
            (store_source_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """INSERT INTO store_tannei_import_guards
                (store_source_id,source_identity,revision,fingerprint)
                VALUES(?,?,1,?)""",
                (store_source_id, source_identity, fingerprint),
            )
            return True
        if row["source_identity"] != source_identity:
            raise StoreError("Source identity changed; explicit review is required")
        if row["fingerprint"] == fingerprint:
            return False
        connection.execute(
            """UPDATE store_tannei_import_guards
            SET revision=revision+1,fingerprint=?,last_legacy_audit_id=NULL WHERE id=?""",
            (fingerprint, row["id"]),
        )
        return True

    def counts(self) -> dict[str, int]:
        """Return non-personal operational directory/import counters."""

        with self.connection() as connection:
            result = {
                name: connection.execute(sql).fetchone()[0]
                for name, sql in {
                    "merchants": "SELECT count(*) FROM store_merchants WHERE archived=0",
                    "mcc_facts": "SELECT count(*) FROM store_facts f "
                    "JOIN store_merchants m ON m.id=f.merchant_id "
                    "WHERE f.archived=0 AND m.archived=0",
                    "source_stores": "SELECT count(*) FROM store_sources WHERE source='tannei'",
                    "evidence": "SELECT count(*) FROM store_evidence "
                    "WHERE revoked=0 AND source<>'tannei'",
                }.items()
            }
            for row in connection.execute(
                """SELECT s.* FROM store_tannei_snapshots s
                JOIN store_brands b ON b.id=s.brand_id WHERE b.archived=0"""
            ):
                snapshot = self._public_snapshot(row)
                result["evidence"] += sum(
                    value["support_count"]
                    for channel in snapshot["channels"].values()
                    for value in channel.values()
                )
            return result
