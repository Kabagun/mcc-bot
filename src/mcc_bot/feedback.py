"""Private feedback drafts, frozen submissions and durable per-part delivery intent."""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from dataclasses import dataclass

from .stores import StoreRepository

MAX_FEEDBACK_TEXT = 3000
MAX_FEEDBACK_PHOTOS = 10


class FeedbackError(ValueError):
    """A bounded input or feedback state cannot be accepted."""


class StaleFeedback(FeedbackError):
    """The draft no longer matches the displayed action."""


@dataclass(frozen=True, slots=True)
class FeedbackPhoto:
    file_id: str
    unique_id: str


@dataclass(frozen=True, slots=True)
class FeedbackDraft:
    id: str
    user_id: int
    version: int
    text: str
    photos: tuple[FeedbackPhoto, ...]
    message: tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class FeedbackDeliveryPart:
    submission_id: str
    position: int
    kind: str
    state: str
    file_id: str | None
    unique_id: str | None


@dataclass(frozen=True, slots=True)
class FeedbackSubmission:
    id: str
    draft_id: str
    user_id: int
    version: int
    text: str
    photos: tuple[FeedbackPhoto, ...]
    username: str | None
    first_name: str | None
    last_name: str | None
    message: tuple[int, int] | None
    parts: tuple[FeedbackDeliveryPart, ...]

    @property
    def delivery_state(self) -> str:
        """Treat interrupted sends as uncertain, never as safely retryable."""

        if all(part.state == "sent" for part in self.parts):
            return "sent"
        if any(part.state in {"sending", "uncertain"} for part in self.parts):
            return "uncertain"
        return "pending"


def _user_id(value: int) -> None:
    if type(value) is not int or not 0 < value < 2**63:
        raise FeedbackError("Feedback requires a private user account")


def text_units(value: str) -> int:
    """Measure Telegram's UTF-16 character budget, including astral emoji."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in value)


def _text(value: str) -> str:
    if not isinstance(value, str):
        raise FeedbackError("Feedback text must be text")
    value = value.replace("\r\n", "\n").strip()
    if text_units(value) > MAX_FEEDBACK_TEXT:
        raise FeedbackError("Feedback text must not exceed 3000 characters")
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise FeedbackError("Feedback text contains unsupported control characters")
    return value


def _reference(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(ord(char) < 33 for char in value)
    ):
        raise FeedbackError("Photo reference is invalid")
    return value


def _profile(value: str | None, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        return None
    return value


class FeedbackRepository:
    """Keep feedback separate from community roles, drafts, facts and audit records."""

    def __init__(self, stores: StoreRepository) -> None:
        self.stores = stores

    def initialize(self) -> None:
        """Create additive private feedback tables in the existing stores database."""

        self.stores.initialize()
        with self.stores.transaction() as conn:
            for statement in (
                """CREATE TABLE IF NOT EXISTS feedback_drafts (
                   user_id INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
                   version INTEGER NOT NULL, text TEXT NOT NULL DEFAULT '',
                   chat_id INTEGER, message_id INTEGER,
                   created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS feedback_draft_photos (
                   draft_id TEXT NOT NULL REFERENCES feedback_drafts(id) ON DELETE CASCADE,
                   position INTEGER NOT NULL, file_id TEXT NOT NULL, unique_id TEXT NOT NULL,
                   PRIMARY KEY(draft_id,position), UNIQUE(draft_id,unique_id))""",
                """CREATE TABLE IF NOT EXISTS feedback_submissions (
                   id TEXT PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE,
                   user_id INTEGER NOT NULL, version INTEGER NOT NULL, text TEXT NOT NULL,
                   username TEXT, first_name TEXT, last_name TEXT,
                   chat_id INTEGER, message_id INTEGER, created_at REAL NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS feedback_delivery_parts (
                   submission_id TEXT NOT NULL REFERENCES feedback_submissions(id),
                   position INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('text','photo')),
                   state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','uncertain')),
                   file_id TEXT, unique_id TEXT, updated_at REAL NOT NULL,
                   PRIMARY KEY(submission_id,position))""",
                """CREATE TABLE IF NOT EXISTS feedback_accepted_inputs (
                   update_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                   draft_id TEXT NOT NULL, created_at REAL NOT NULL)""",
                """CREATE INDEX IF NOT EXISTS feedback_submissions_user
                   ON feedback_submissions(user_id,created_at)""",
            ):
                conn.execute(statement)

    @staticmethod
    def _draft(conn: sqlite3.Connection, user_id: int) -> FeedbackDraft | None:
        row = conn.execute("SELECT * FROM feedback_drafts WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            return None
        photos = tuple(
            FeedbackPhoto(item["file_id"], item["unique_id"])
            for item in conn.execute(
                """SELECT file_id,unique_id FROM feedback_draft_photos
                   WHERE draft_id=? ORDER BY position""",
                (row["id"],),
            )
        )
        message = (row["chat_id"], row["message_id"]) if row["message_id"] is not None else None
        return FeedbackDraft(
            row["id"], row["user_id"], row["version"], row["text"], photos, message
        )

    def _current(
        self, conn: sqlite3.Connection, user_id: int, draft_id: str | None, version: int | None
    ) -> FeedbackDraft:
        _user_id(user_id)
        if version is not None and (type(version) is not int or version < 0):
            raise StaleFeedback("Feedback draft version is invalid")
        draft = self._draft(conn, user_id)
        if (
            draft is None
            or (draft_id is not None and draft.id != draft_id)
            or (version is not None and draft.version != version)
        ):
            raise StaleFeedback("Feedback draft changed; open the current form")
        return draft

    def draft(self, user_id: int) -> FeedbackDraft | None:
        _user_id(user_id)
        with self.stores.transaction() as conn:
            return self._draft(conn, user_id)

    def begin(self, user_id: int) -> FeedbackDraft:
        """Resume the single existing form or create an empty one."""

        _user_id(user_id)
        with self.stores.transaction() as conn:
            now = time.time()
            conn.execute(
                """INSERT OR IGNORE INTO feedback_drafts(user_id,id,version,created_at,updated_at)
                   VALUES(?,?,0,?,?)""",
                (user_id, uuid.uuid4().hex, now, now),
            )
            return self._current(conn, user_id, None, None)

    def set_text(
        self,
        user_id: int,
        text: str,
        *,
        draft_id: str | None = None,
        expected_version: int | None = None,
    ) -> FeedbackDraft:
        value = _text(text)
        with self.stores.transaction() as conn:
            draft = self._current(conn, user_id, draft_id, expected_version)
            if value != draft.text:
                conn.execute(
                    """UPDATE feedback_drafts SET text=?,version=version+1,updated_at=?
                       WHERE user_id=?""",
                    (value, time.time(), user_id),
                )
            return self._current(conn, user_id, draft.id, None)

    def append_text(
        self,
        user_id: int,
        text: str,
        *,
        draft_id: str | None = None,
        expected_version: int | None = None,
        update_id: int | None = None,
    ) -> FeedbackDraft:
        return self.append_input(
            user_id,
            text=text,
            draft_id=draft_id,
            expected_version=expected_version,
            update_id=update_id,
        )

    def add_photo(
        self,
        user_id: int,
        file_id: str,
        unique_id: str,
        *,
        draft_id: str | None = None,
        expected_version: int | None = None,
        update_id: int | None = None,
    ) -> FeedbackDraft:
        return self.append_input(
            user_id,
            file_id=file_id,
            unique_id=unique_id,
            draft_id=draft_id,
            expected_version=expected_version,
            update_id=update_id,
        )

    def append_input(
        self,
        user_id: int,
        *,
        text: str = "",
        file_id: str | None = None,
        unique_id: str | None = None,
        update_id: int | None = None,
        draft_id: str | None = None,
        expected_version: int | None = None,
    ) -> FeedbackDraft:
        """Atomically accept one text/photo/caption input and its replay guard.

        Accepted update IDs survive draft deletion so a delayed Telegram replay
        cannot contaminate a later form. Replays return the active draft unchanged;
        if its form has already closed, they raise StaleFeedback instead.
        """

        _user_id(user_id)
        if update_id is not None and (type(update_id) is not int or not 0 <= update_id < 2**63):
            raise FeedbackError("Feedback input update is invalid")
        with self.stores.transaction() as conn:
            if update_id is not None:
                accepted = conn.execute(
                    "SELECT user_id FROM feedback_accepted_inputs WHERE update_id=?", (update_id,)
                ).fetchone()
                if accepted is not None:
                    if accepted["user_id"] != user_id:
                        raise StaleFeedback("Feedback input no longer matches this form")
                    return self._current(conn, user_id, None, None)
            draft = self._current(conn, user_id, draft_id, expected_version)
            value = _text(text)
            combined = _text("\n\n".join(part for part in (draft.text, value) if part))
            if (file_id is None) != (unique_id is None):
                raise FeedbackError("Photo reference is incomplete")
            new_photo = False
            if file_id is not None:
                file_id, unique_id = _reference(file_id), _reference(unique_id)
                new_photo = not any(photo.unique_id == unique_id for photo in draft.photos)
                if new_photo and len(draft.photos) >= MAX_FEEDBACK_PHOTOS:
                    raise FeedbackError("Feedback supports at most 10 photos")
            now = time.time()
            if update_id is not None:
                conn.execute(
                    "INSERT INTO feedback_accepted_inputs VALUES(?,?,?,?)",
                    (update_id, user_id, draft.id, now),
                )
            if new_photo:
                conn.execute(
                    "INSERT INTO feedback_draft_photos VALUES(?,?,?,?)",
                    (draft.id, len(draft.photos), file_id, unique_id),
                )
            if new_photo or combined != draft.text:
                conn.execute(
                    """UPDATE feedback_drafts SET text=?,version=version+1,updated_at=?
                       WHERE user_id=?""",
                    (combined, now, user_id),
                )
            return self._current(conn, user_id, draft.id, None)

    def clear_photos(
        self, user_id: int, *, draft_id: str | None = None, expected_version: int | None = None
    ) -> FeedbackDraft:
        with self.stores.transaction() as conn:
            draft = self._current(conn, user_id, draft_id, expected_version)
            if draft.photos:
                conn.execute("DELETE FROM feedback_draft_photos WHERE draft_id=?", (draft.id,))
                conn.execute(
                    "UPDATE feedback_drafts SET version=version+1,updated_at=? WHERE user_id=?",
                    (time.time(), user_id),
                )
            return self._current(conn, user_id, draft.id, None)

    def bind_message(
        self, user_id: int, chat_id: int, message_id: int, *, draft_id: str | None = None
    ) -> FeedbackDraft:
        _user_id(chat_id)
        if type(message_id) is not int or not 0 < message_id < 2**63:
            raise FeedbackError("Form message is invalid")
        if chat_id != user_id:
            raise FeedbackError("Feedback form must stay in its author's private chat")
        with self.stores.transaction() as conn:
            draft = self._current(conn, user_id, draft_id, None)
            conn.execute(
                "UPDATE feedback_drafts SET chat_id=?,message_id=? WHERE user_id=?",
                (chat_id, message_id, user_id),
            )
            return self._current(conn, user_id, draft.id, None)

    def discard(self, user_id: int, *, draft_id: str | None = None) -> None:
        _user_id(user_id)
        with self.stores.transaction() as conn:
            current = self._draft(conn, user_id)
            if current is None:
                return
            if draft_id is not None and current.id != draft_id:
                raise StaleFeedback("Feedback draft changed; open the current form")
            conn.execute("DELETE FROM feedback_drafts WHERE user_id=?", (user_id,))

    @staticmethod
    def _parts(conn: sqlite3.Connection, submission_id: str) -> tuple[FeedbackDeliveryPart, ...]:
        return tuple(
            FeedbackDeliveryPart(**dict(row))
            for row in conn.execute(
                """SELECT submission_id,position,kind,state,file_id,unique_id
                   FROM feedback_delivery_parts WHERE submission_id=? ORDER BY position""",
                (submission_id,),
            )
        )

    def _submission(self, conn: sqlite3.Connection, submission_id: str) -> FeedbackSubmission:
        row = conn.execute(
            "SELECT * FROM feedback_submissions WHERE id=?", (submission_id,)
        ).fetchone()
        if row is None:
            raise StaleFeedback("Feedback submission is unavailable")
        parts = self._parts(conn, submission_id)
        photos = tuple(
            FeedbackPhoto(part.file_id, part.unique_id) for part in parts if part.kind == "photo"
        )
        message = (row["chat_id"], row["message_id"]) if row["message_id"] is not None else None
        return FeedbackSubmission(
            row["id"],
            row["draft_id"],
            row["user_id"],
            row["version"],
            row["text"],
            photos,
            row["username"],
            row["first_name"],
            row["last_name"],
            message,
            parts,
        )

    def submit(
        self,
        user_id: int,
        draft_id: str,
        expected_version: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> FeedbackSubmission:
        """Freeze content and all pending delivery parts atomically before any send.

        Repeating the same successful submit callback returns its original submission,
        even if the user has since opened a new draft. It never freezes that new form.
        """

        _user_id(user_id)
        if not isinstance(draft_id, str) or not draft_id:
            raise StaleFeedback("Feedback draft identity is invalid")
        if type(expected_version) is not int or expected_version < 0:
            raise StaleFeedback("Feedback draft version is invalid")
        with self.stores.transaction() as conn:
            prior = conn.execute(
                "SELECT id,user_id,version FROM feedback_submissions WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if prior is not None:
                if prior["user_id"] != user_id or prior["version"] != expected_version:
                    raise StaleFeedback("Feedback action no longer matches this submission")
                return self._submission(conn, prior["id"])
            draft = self._current(conn, user_id, draft_id, expected_version)
            if not draft.text and not draft.photos:
                raise FeedbackError("Add text or at least one photo before sending feedback")
            name = _profile(username, 33)
            if name is not None:
                name = name.removeprefix("@")
                if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", name):
                    name = None
            now, submission_id = time.time(), uuid.uuid4().hex
            conn.execute(
                """INSERT INTO feedback_submissions VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    submission_id,
                    draft.id,
                    user_id,
                    draft.version,
                    draft.text,
                    name,
                    _profile(first_name, 128),
                    _profile(last_name, 128),
                    *(draft.message or (None, None)),
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO feedback_delivery_parts VALUES(?,0,'text','pending',NULL,NULL,?)",
                (submission_id, now),
            )
            conn.executemany(
                "INSERT INTO feedback_delivery_parts VALUES(?,?,'photo','pending',?,?,?)",
                (
                    (submission_id, position, photo.file_id, photo.unique_id, now)
                    for position, photo in enumerate(draft.photos, 1)
                ),
            )
            conn.execute("DELETE FROM feedback_drafts WHERE user_id=?", (user_id,))
            return self._submission(conn, submission_id)

    def submission(self, submission_id: str, *, user_id: int | None = None) -> FeedbackSubmission:
        """Load frozen feedback, optionally enforcing its author's identity."""

        if user_id is not None:
            _user_id(user_id)
        with self.stores.transaction() as conn:
            result = self._submission(conn, submission_id)
            if user_id is not None and result.user_id != user_id:
                raise StaleFeedback("Feedback submission is unavailable")
            return result

    def delivery_parts(self, submission_id: str) -> tuple[FeedbackDeliveryPart, ...]:
        with self.stores.transaction() as conn:
            return self._submission(conn, submission_id).parts

    def reserve_delivery_part(
        self, submission_id: str, position: int
    ) -> FeedbackDeliveryPart | None:
        """Persist send intent in order; block a submission with any ambiguous part."""

        with self.stores.transaction() as conn:
            parts = self._submission(conn, submission_id).parts
            if any(part.state in {"sending", "uncertain"} for part in parts):
                return None
            target = next((part for part in parts if part.position == position), None)
            if target is None or target.state != "pending":
                return None
            if any(part.position < position and part.state != "sent" for part in parts):
                return None
            conn.execute(
                """UPDATE feedback_delivery_parts SET state='sending',updated_at=?
                   WHERE submission_id=? AND position=?""",
                (time.time(), submission_id, position),
            )
            return next(
                part for part in self._parts(conn, submission_id) if part.position == position
            )

    def finish_delivery_part(self, submission_id: str, position: int, state: str) -> None:
        """Acknowledge a send or ambiguity; pending is only for a definite rejection.

        Never return a timeout or interrupted request to pending: Telegram may have
        accepted it. The caller must use uncertain for every ambiguous outcome.
        """

        if state not in {"sent", "uncertain", "pending"}:
            raise FeedbackError("Feedback delivery outcome is invalid")
        with self.stores.transaction() as conn:
            conn.execute(
                """UPDATE feedback_delivery_parts SET state=?,updated_at=?
                   WHERE submission_id=? AND position=? AND state='sending'""",
                (state, time.time(), submission_id, position),
            )
