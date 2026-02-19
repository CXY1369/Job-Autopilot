from autojobagent.core.intent_engine import (
    fallback_label_intents,
    infer_label_intents,
    infer_snapshot_intents,
    infer_text_intents,
)
from autojobagent.core.ui_snapshot import SnapshotItem


def test_fallback_label_intents_basic():
    intents = fallback_label_intents("Submit Application")
    assert "progression_action" in intents
    assert "apply_entry" in intents

    upload_intents = fallback_label_intents("Upload Resume")
    assert "upload_request" in upload_intents


def test_infer_label_intents_cache_and_merge():
    cache: dict[str, dict[str, list[str]]] = {}

    def fake_llm(labels, context):
        assert context == "ctx"
        return {labels[0]: {"apply_entry"}}

    result = infer_label_intents(
        ["Apply Now", "Apply Now", "Continue"],
        context="ctx",
        intent_cache=cache,
        infer_label_intents_with_llm_fn=fake_llm,
    )
    assert "apply_entry" in result["Apply Now"]
    assert "progression_action" in result["Continue"]

    result_2 = infer_label_intents(
        ["Apply Now", "Continue"],
        context="ctx",
        intent_cache=cache,
        infer_label_intents_with_llm_fn=lambda *_: {"bad": {"bad"}},
    )
    assert result_2 == result


def test_infer_snapshot_intents_maps_refs():
    snapshot_map = {
        "e1": SnapshotItem(ref="e1", role="button", name="Apply", nth=0),
        "e2": SnapshotItem(ref="e2", role="link", name="Sign in", nth=0),
        "e3": SnapshotItem(ref="e3", role="textbox", name="Name", nth=0),
    }

    def fake_infer(labels, context=""):
        assert "Apply" in labels and "Sign in" in labels
        return {"Apply": {"apply_entry"}, "Sign in": {"login_action"}}

    mapped = infer_snapshot_intents(
        snapshot_map,
        "text",
        infer_label_intents_fn=fake_infer,
    )
    assert mapped["e1"] == {"apply_entry"}
    assert mapped["e2"] == {"login_action"}
    assert "e3" not in mapped


def test_infer_text_intents_fallback_keywords():
    cache: dict[str, dict[str, list[str]]] = {}
    intents = infer_text_intents(
        "Please upload your resume and attach a cv",
        intent_cache=cache,
        client=None,
        intent_model="",
        safe_parse_json_fn=None,
    )
    assert "upload_request" in intents
