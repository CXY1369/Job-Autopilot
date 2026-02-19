from autojobagent.core.manual_gate import (
    classify_page_state,
    select_apply_entry_candidate,
)
from autojobagent.core.ui_snapshot import SnapshotItem


def test_classify_page_state_job_detail_with_apply():
    snapshot_map = {
        "e1": SnapshotItem(ref="e1", role="button", name="Apply", nth=0),
    }
    page_state, stats = classify_page_state(
        snapshot_map=snapshot_map,
        evidence={"has_apply_cta": True},
        manual_required=False,
        current_url="https://jobs.example.com/job/123",
    )
    assert page_state == "job_detail_with_apply"
    assert stats["has_apply_cta"] is True


def test_classify_page_state_application_url_priority():
    snapshot_map = {}
    page_state, _ = classify_page_state(
        snapshot_map=snapshot_map,
        evidence={"has_apply_cta": False},
        manual_required=False,
        current_url="https://jobs.ashbyhq.com/acme/abcd/application",
    )
    assert page_state == "application_or_form_page"


def test_select_apply_entry_candidate_filters_noise():
    snapshot_map = {
        "e1": SnapshotItem(ref="e1", role="button", name="Upload File", nth=0),
        "e2": SnapshotItem(ref="e2", role="button", name="Apply", nth=0),
        "e3": SnapshotItem(ref="e3", role="link", name="Apply Here", nth=0),
    }
    snapshot_intents = {
        "e1": {"apply_entry"},
        "e2": {"apply_entry"},
        "e3": {"apply_entry"},
    }
    picked = select_apply_entry_candidate(
        snapshot_map=snapshot_map,
        snapshot_intents=snapshot_intents,
        current_url="https://jobs.example.com/role/123",
    )
    assert picked is not None
    assert picked.ref == "e2"
