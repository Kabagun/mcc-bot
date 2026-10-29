"""Atomic pilot submission preserves editable state and publication safeguards."""

import pytest

from mcc_bot.community import CommunityError, CommunityService, StaleAction
from mcc_bot.stores import StoreRepository


@pytest.fixture
def service(tmp_path):
    service = CommunityService(StoreRepository(tmp_path / "stores.sqlite3"), owner_id=1)
    service.initialize()
    service.set_role(1, 2, True)
    return service


def make_form(service, user_id=10, *, form="mcc_save", values=None, media=False):
    values = values or {"name": "Pilot shop", "mcc": "5411", "channel": "offline"}
    draft = service.begin(
        user_id,
        stage="form_editor",
        data={"form_flow_v2": True, "draft_mode": True, "form": form, "values": values},
    )
    if media:
        draft = service.advance(
            user_id,
            draft.id,
            draft.version,
            "form_editor",
            draft.data,
            media=("synthetic-file", "synthetic-photo"),
        )
    service.bind_editor_message(user_id, draft.id, user_id, 100)
    return draft


def submit_form(service, draft, payload=None):
    return service.submit_form(
        draft.user_id, draft.id, draft.version, draft.data["form"], payload or draft.data["values"]
    )


def test_duplicate_retains_form_version_media_and_binding_and_can_retry(service):
    submitted = make_form(service)
    submit_form(service, submitted)
    draft = make_form(service, 11, media=True)

    with pytest.raises(CommunityError, match="уже"):
        submit_form(service, draft)

    assert service.draft(11) == draft
    assert service.editor_message(11, draft.id) == (11, 100)
    assert service.draft_has_media(11, draft.id)
    data = {**draft.data, "values": {**draft.data["values"], "name": "Other pilot shop"}}
    draft = service.advance(11, draft.id, draft.version, "form_editor", data)
    proposal = submit_form(service, draft)
    assert proposal.status == "pending"
    assert proposal.payload["name"] == "Other pilot shop"
    assert service.proposal_has_media(11, proposal.id)
    assert service.draft(11) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Pilot shop", "mcc": "54", "channel": "offline"},
        {"name": "Pilot shop", "mcc": "5411", "channel": "invalid"},
        {"name": "Pilot shop", "channel": "offline"},
    ],
)
def test_invalid_payload_leaves_full_form_unchanged(service, payload):
    draft = make_form(service, media=True)
    with pytest.raises(CommunityError):
        submit_form(service, draft, payload)
    assert service.draft(10) == draft
    assert service.draft_has_media(10, draft.id)
    assert service.editor_message(10, draft.id) == (10, 100)


def test_failed_publication_rolls_back_every_write(service, monkeypatch):
    draft = make_form(service, 2, media=True)
    publish = service._publish

    def failing_publish(*args, **kwargs):
        publish(*args, **kwargs)
        raise CommunityError("Synthetic publication failure")

    monkeypatch.setattr(service, "_publish", failing_publish)
    with pytest.raises(CommunityError, match="Synthetic"):
        submit_form(service, draft)

    assert service.draft(2) == draft
    assert service.draft_has_media(2, draft.id)
    assert service.editor_message(2, draft.id) == (2, 100)
    assert not service.stores.search("Pilot shop").matches
    with service.stores.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM community_proposals").fetchone()[0] == 0


def test_source_changed_after_form_open_cannot_be_overwritten(service):
    created = service.stores.apply_change(
        "add_merchant", {"name": "Original shop", "channel": "offline", "mcc": "5411"}, 1
    )
    draft = make_form(
        service,
        2,
        form="store_metadata",
        values={"brand_id": created.brand_id, "name": "Pilot edit", "aliases": []},
    )
    service.stores.apply_change(
        "edit_brand_names",
        {"brand_id": created.brand_id, "name": "Concurrent edit", "aliases": []},
        1,
    )

    with pytest.raises(StaleAction, match="изменились"):
        submit_form(service, draft)
    assert service.draft(2) == draft
    assert service.stores.get_brand(created.brand_id).name == "Concurrent edit"


def test_new_fact_has_no_edit_snapshot_and_revoked_role_cannot_publish(service):
    draft = make_form(service, 2)
    assert "form_expected" not in draft.data
    service.set_role(1, 2, False)
    with pytest.raises(StaleAction):
        submit_form(service, draft)
    assert not service.stores.search("Pilot shop").matches


def test_legacy_form_cannot_use_atomic_pilot_entrypoint(service):
    draft = service.begin(
        10,
        stage="form_editor",
        data={"form": "mcc_save", "values": {"name": "Legacy"}},
    )
    with pytest.raises(StaleAction):
        service.submit_form(10, draft.id, draft.version, "mcc_save", {})
    assert service.draft(10) == draft


def test_old_version_cannot_submit_and_new_version_submits_once(service):
    draft = make_form(service, 2)
    current = service.advance(2, draft.id, draft.version, "form_editor", draft.data)
    with pytest.raises(StaleAction):
        submit_form(service, draft)
    proposal = submit_form(service, current)
    assert proposal.status == "approved"
    with pytest.raises(StaleAction):
        submit_form(service, current)
