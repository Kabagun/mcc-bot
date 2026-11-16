"""Private feedback persistence, validation and conservative delivery recovery."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from mcc_bot.feedback import (
    MAX_FEEDBACK_PHOTOS,
    MAX_FEEDBACK_TEXT,
    FeedbackError,
    FeedbackRepository,
    StaleFeedback,
)
from mcc_bot.stores import StoreRepository


@pytest.fixture
def repository(tmp_path):
    value = FeedbackRepository(StoreRepository(tmp_path / "stores.sqlite3"))
    value.initialize()
    return value


def ready(repository, *, photos=2, text="Synthetic feedback"):
    draft = repository.begin(10)
    draft = repository.set_text(10, text)
    for index in range(photos):
        draft = repository.add_photo(10, f"synthetic-file-{index}", f"synthetic-unique-{index}")
    return draft


def test_astral_emoji_use_telegram_character_budget(repository):
    draft = repository.begin(10)
    draft = repository.set_text(10, "😀" * 1500)
    assert len(draft.text) == 1500
    with pytest.raises(FeedbackError):
        repository.append_text(10, "x")
    assert repository.draft(10) == draft


def submitted(repository, *, photos=2):
    draft = ready(repository, photos=photos)
    return repository.submit(
        10,
        draft.id,
        draft.version,
        username="synthetic_user",
        first_name="Synthetic",
        last_name="User",
    )


def test_initialization_adds_tables_without_changing_existing_data(tmp_path):
    stores = StoreRepository(tmp_path / "existing.sqlite3")
    stores.initialize()
    with stores.transaction() as conn:
        conn.execute("CREATE TABLE fixture_existing(id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO fixture_existing VALUES(1,'synthetic preserved data')")
    repository = FeedbackRepository(stores)
    repository.initialize()
    draft = ready(repository)
    repository.initialize()

    assert repository.draft(10) == draft
    with stores.connection() as conn:
        assert tuple(conn.execute("SELECT * FROM fixture_existing").fetchone()) == (
            1,
            "synthetic preserved data",
        )
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "feedback_drafts",
        "feedback_draft_photos",
        "feedback_submissions",
        "feedback_delivery_parts",
    } <= tables


def test_begin_resumes_single_draft_and_keeps_users_separate(repository):
    assert repository.draft(10) is None
    draft = ready(repository)
    assert repository.begin(10) == draft
    other = repository.begin(20)
    assert other.id != draft.id
    assert (other.text, other.photos, other.message) == ("", (), None)
    with pytest.raises(FrozenInstanceError):
        draft.text = "changed"


@pytest.mark.parametrize("user_id", [0, -1, True, "10", 2**63])
def test_invalid_user_identity_is_rejected(repository, user_id):
    with pytest.raises(FeedbackError):
        repository.begin(user_id)


def test_text_limit_append_and_clear_are_atomic(repository):
    draft = repository.begin(10)
    draft = repository.set_text(10, "x" * MAX_FEEDBACK_TEXT)
    assert len(draft.text) == MAX_FEEDBACK_TEXT
    with pytest.raises(FeedbackError, match="3000"):
        repository.set_text(10, "x" * (MAX_FEEDBACK_TEXT + 1))
    with pytest.raises(FeedbackError, match="3000"):
        repository.append_text(10, "more")
    assert repository.draft(10) == draft
    draft = repository.set_text(10, " First\r\nline ")
    draft = repository.append_text(10, " Second ")
    assert draft.text == "First\nline\n\nSecond"
    draft = repository.set_text(10, "")
    assert draft.text == ""
    unchanged = repository.set_text(10, "  ")
    assert unchanged == draft


def test_photo_limit_deduplication_order_and_clear(repository):
    draft = ready(repository, photos=MAX_FEEDBACK_PHOTOS)
    assert [photo.unique_id for photo in draft.photos] == [
        f"synthetic-unique-{index}" for index in range(MAX_FEEDBACK_PHOTOS)
    ]
    same = repository.add_photo(10, "different-file-for-same-photo", "synthetic-unique-0")
    assert same == draft
    with pytest.raises(FeedbackError, match="10 photos"):
        repository.add_photo(10, "extra-file", "extra-unique")
    assert repository.draft(10) == draft
    cleared = repository.clear_photos(10)
    assert cleared.photos == ()
    assert cleared.version == draft.version + 1
    added = repository.add_photo(10, "new-file", "new-unique")
    assert added.photos[0].unique_id == "new-unique"


@pytest.mark.parametrize("reference", ["", "bad reference", "x" * 1025, "bad\nreference"])
def test_invalid_photo_references_leave_draft_unchanged(repository, reference):
    draft = repository.begin(10)
    with pytest.raises(FeedbackError):
        repository.add_photo(10, reference, "synthetic-unique")
    assert repository.draft(10) == draft


def test_empty_feedback_cannot_submit_but_photo_only_can(repository):
    draft = repository.begin(10)
    with pytest.raises(FeedbackError, match="Add text or"):
        repository.submit(10, draft.id, draft.version)
    assert repository.draft(10) == draft
    draft = repository.add_photo(10, "synthetic-file", "synthetic-unique")
    value = repository.submit(10, draft.id, draft.version)

    assert value.text == ""
    assert [part.kind for part in value.parts] == ["text", "photo"]
    assert [part.state for part in value.parts] == ["pending", "pending"]
    assert repository.draft(10) is None


def test_stale_content_version_and_previous_draft_cannot_submit_new_content(repository):
    first = repository.begin(10)
    latest = repository.set_text(
        10, "Updated text", draft_id=first.id, expected_version=first.version
    )
    with pytest.raises(StaleFeedback):
        repository.submit(10, first.id, first.version)
    with pytest.raises(StaleFeedback):
        repository.add_photo(
            10,
            "synthetic-file",
            "synthetic-unique",
            draft_id=first.id,
            expected_version=first.version,
        )
    assert repository.draft(10) == latest
    repository.discard(10, draft_id=latest.id)
    replacement = ready(repository, text="Replacement")
    with pytest.raises(StaleFeedback):
        repository.submit(10, first.id, replacement.version)
    with pytest.raises(StaleFeedback):
        repository.discard(10, draft_id=first.id)
    assert repository.draft(10) == replacement


def test_bound_message_does_not_stale_content_and_is_frozen_with_submission(repository):
    draft = ready(repository)
    bound = repository.bind_message(10, 10, 100, draft_id=draft.id)
    assert bound.version == draft.version
    assert bound.message == (10, 100)
    assert repository.bind_message(10, 10, 101, draft_id=draft.id).message == (10, 101)
    with pytest.raises(FeedbackError):
        repository.bind_message(10, 20, 100)
    frozen = repository.submit(10, draft.id, draft.version)
    assert frozen.message == (10, 101)


def test_submit_freezes_profile_content_and_all_parts_before_any_delivery(repository):
    draft = ready(repository, photos=3)
    frozen = repository.submit(
        10,
        draft.id,
        draft.version,
        username="@synthetic_user",
        first_name="Synthetic",
        last_name="User",
    )

    assert (frozen.username, frozen.first_name, frozen.last_name) == (
        "synthetic_user",
        "Synthetic",
        "User",
    )
    assert frozen.text == draft.text
    assert frozen.photos == draft.photos
    assert frozen.delivery_state == "pending"
    assert [part.position for part in frozen.parts] == [0, 1, 2, 3]
    assert [part.file_id for part in frozen.parts] == [
        None,
        "synthetic-file-0",
        "synthetic-file-1",
        "synthetic-file-2",
    ]
    assert all(part.state == "pending" for part in frozen.parts)
    assert repository.draft(10) is None
    assert repository.submission(frozen.id, user_id=10) == frozen
    with pytest.raises(StaleFeedback):
        repository.submission(frozen.id, user_id=20)


def test_duplicate_submit_returns_original_without_affecting_new_draft(repository):
    frozen = submitted(repository)
    new_draft = ready(repository, text="Next feedback")
    repeated = repository.submit(
        10, frozen.draft_id, frozen.version, username="changed_name", first_name="Changed"
    )

    assert repeated == frozen
    assert repository.draft(10) == new_draft
    with pytest.raises(StaleFeedback):
        repository.submit(20, frozen.draft_id, frozen.version)
    with pytest.raises(StaleFeedback):
        repository.submit(10, frozen.draft_id, frozen.version - 1)
    with repository.stores.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback_submissions").fetchone()[0] == 1


def test_concurrent_submit_freezes_once(repository):
    draft = ready(repository)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(
            executor.map(lambda _: repository.submit(10, draft.id, draft.version), range(3))
        )
    assert len({value.id for value in results}) == 1
    with repository.stores.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback_delivery_parts").fetchone()[0] == 3


def test_definite_rejection_can_resume_pending_parts_without_repeating_success(repository):
    frozen = submitted(repository)
    assert repository.reserve_delivery_part(frozen.id, 1) is None
    header = repository.reserve_delivery_part(frozen.id, 0)
    assert header.state == "sending"
    repository.finish_delivery_part(frozen.id, 0, "sent")
    first = repository.reserve_delivery_part(frozen.id, 1)
    assert first.state == "sending"
    repository.finish_delivery_part(frozen.id, 1, "pending")
    restarted = FeedbackRepository(repository.stores)
    restarted.initialize()

    assert restarted.reserve_delivery_part(frozen.id, 0) is None
    assert restarted.reserve_delivery_part(frozen.id, 1).state == "sending"
    restarted.finish_delivery_part(frozen.id, 1, "sent")
    assert restarted.reserve_delivery_part(frozen.id, 2).state == "sending"
    restarted.finish_delivery_part(frozen.id, 2, "sent")
    assert restarted.submission(frozen.id).delivery_state == "sent"
    assert restarted.reserve_delivery_part(frozen.id, 2) is None


@pytest.mark.parametrize("persist_outcome", [False, True])
def test_interrupted_and_uncertain_sends_never_resume_automatically(repository, persist_outcome):
    frozen = submitted(repository)
    repository.reserve_delivery_part(frozen.id, 0)
    repository.finish_delivery_part(frozen.id, 0, "sent")
    repository.reserve_delivery_part(frozen.id, 1)
    if persist_outcome:
        repository.finish_delivery_part(frozen.id, 1, "uncertain")
    restarted = FeedbackRepository(repository.stores)
    restarted.initialize()

    assert restarted.submission(frozen.id).delivery_state == "uncertain"
    assert restarted.reserve_delivery_part(frozen.id, 0) is None
    assert restarted.reserve_delivery_part(frozen.id, 1) is None
    assert restarted.reserve_delivery_part(frozen.id, 2) is None
    assert restarted.submit(10, frozen.draft_id, frozen.version).id == frozen.id


def test_concurrent_delivery_reservation_has_one_winner(repository):
    frozen = submitted(repository, photos=0)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(
            executor.map(lambda _: repository.reserve_delivery_part(frozen.id, 0), range(3))
        )
    assert sum(result is not None for result in results) == 1
    assert repository.submission(frozen.id).delivery_state == "uncertain"


def test_terminal_states_cannot_be_overwritten_and_invalid_outcomes_fail(repository):
    frozen = submitted(repository, photos=0)
    with pytest.raises(FeedbackError):
        repository.finish_delivery_part(frozen.id, 0, "failed")
    repository.finish_delivery_part(frozen.id, 0, "sent")
    assert repository.delivery_parts(frozen.id)[0].state == "pending"
    repository.reserve_delivery_part(frozen.id, 0)
    repository.finish_delivery_part(frozen.id, 0, "sent")
    repository.finish_delivery_part(frozen.id, 0, "pending")
    repository.finish_delivery_part(frozen.id, 0, "uncertain")
    assert repository.delivery_parts(frozen.id)[0].state == "sent"


def test_discard_removes_only_private_draft_refs_not_submitted_feedback(repository):
    frozen = submitted(repository)
    active = ready(repository)
    repository.discard(10, draft_id=active.id)
    repository.discard(10)

    assert repository.draft(10) is None
    assert repository.submission(frozen.id) == frozen
    with repository.stores.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback_draft_photos").fetchone()[0] == 0


def test_caption_photo_and_input_guard_are_accepted_in_one_version(repository):
    draft = repository.begin(10)
    updated = repository.append_input(
        10,
        text="Synthetic caption",
        file_id="synthetic-photo",
        unique_id="synthetic-unique",
        update_id=100,
        draft_id=draft.id,
        expected_version=draft.version,
    )

    assert updated.text == "Synthetic caption"
    assert updated.photos[0].file_id == "synthetic-photo"
    assert updated.version == draft.version + 1
    replay = repository.append_input(
        10,
        text="Synthetic caption",
        file_id="synthetic-photo",
        unique_id="synthetic-unique",
        update_id=100,
        draft_id=draft.id,
        expected_version=draft.version,
    )
    assert replay == updated
    with repository.stores.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback_accepted_inputs").fetchone()[0] == 1


def test_text_limit_rejects_whole_caption_photo_and_does_not_consume_update(repository):
    original = repository.begin(10)
    original = repository.set_text(10, "x" * MAX_FEEDBACK_TEXT)
    with pytest.raises(FeedbackError, match="3000"):
        repository.append_input(
            10,
            text="caption",
            file_id="synthetic-photo",
            unique_id="synthetic-unique",
            update_id=100,
        )
    assert repository.draft(10) == original
    with repository.stores.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback_accepted_inputs").fetchone()[0] == 0
    repository.set_text(10, "")
    accepted = repository.append_input(
        10, text="caption", file_id="synthetic-photo", unique_id="synthetic-unique", update_id=100
    )
    assert (accepted.text, len(accepted.photos)) == ("caption", 1)


def test_photo_limit_rejects_whole_caption_and_preserves_prior_draft(repository):
    original = ready(repository, photos=MAX_FEEDBACK_PHOTOS)
    with pytest.raises(FeedbackError, match="10 photos"):
        repository.append_input(
            10,
            text="must not append",
            file_id="extra-file",
            unique_id="extra-unique",
            update_id=100,
        )
    assert repository.draft(10) == original
    with repository.stores.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback_accepted_inputs").fetchone()[0] == 0


def test_convenience_methods_deduplicate_update_ids_after_restart(repository):
    repository.begin(10)
    repository.append_text(10, "First", update_id=100)
    repository.add_photo(10, "synthetic-file", "synthetic-unique", update_id=101)
    latest = repository.append_text(10, "Second", update_id=102)
    restarted = FeedbackRepository(repository.stores)
    restarted.initialize()

    assert restarted.append_text(10, "First", update_id=100) == latest
    assert restarted.add_photo(10, "synthetic-file", "synthetic-unique", update_id=101) == latest
    assert latest.text == "First\n\nSecond"
    assert len(latest.photos) == 1


def test_accepted_input_cannot_replay_into_another_draft_or_user(repository):
    repository.begin(10)
    first = repository.append_text(10, "Original input", update_id=100)
    repository.submit(10, first.id, first.version)
    with pytest.raises(StaleFeedback):
        repository.append_text(10, "Original input", update_id=100)
    replacement = repository.begin(10)
    assert repository.append_text(10, "Original input", update_id=100) == replacement
    other = repository.begin(20)
    with pytest.raises(StaleFeedback):
        repository.append_text(20, "Original input", update_id=100)
    assert repository.draft(20) == other


def test_concurrent_input_replay_appends_once(repository):
    repository.begin(10)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(
            executor.map(
                lambda _: repository.append_input(
                    10,
                    text="caption",
                    file_id="synthetic-file",
                    unique_id="synthetic-unique",
                    update_id=100,
                ),
                range(3),
            )
        )

    assert len({value.version for value in results}) == 1
    assert results[0].text == "caption"
    assert len(results[0].photos) == 1
