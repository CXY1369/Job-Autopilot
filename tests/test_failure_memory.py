from pathlib import Path

from autojobagent.core.failure_memory import (
    FailureMemoryStore,
    build_failure_signature,
)


def test_failure_memory_upsert_and_query(tmp_path: Path):
    store = FailureMemoryStore(path=tmp_path / "failure_memory.ndjson")
    store.upsert_case(
        page_scope="jobs.ashbyhq.com|/suno/123/application",
        classification="external_blocked",
        reason_code="anti_spam_or_risk_blocked",
        symptom="submission flagged as possible spam",
        root_cause="risk gate",
        successful_strategy="refresh and restart",
        guardrails="do not repeat submit in same blocked page",
        evidence_snippet="flagged as possible spam",
        question_text="",
        action="click",
        selector="Submit Application",
        source_event="submission_outcome_classified",
    )
    # upsert again -> hit_count should increase
    store.upsert_case(
        page_scope="jobs.ashbyhq.com|/suno/123/application",
        classification="external_blocked",
        reason_code="anti_spam_or_risk_blocked",
        symptom="submission flagged as possible spam",
        root_cause="risk gate",
        successful_strategy="refresh and restart",
        guardrails="do not repeat submit in same blocked page",
        action="click",
        selector="Submit Application",
    )

    hits = store.query_similar(
        page_scope="jobs.ashbyhq.com|/suno/9823587b-1ab4-4ecc-a6f1-32f744364bdc/application",
        classification="external_blocked",
        reason_code="anti_spam_or_risk_blocked",
        action="click",
        limit=2,
    )
    assert len(hits) == 1
    assert hits[0].hit_count >= 2
    hint = store.format_hints_for_prompt(hits)
    assert "策略" in hint
    assert "refresh" in hint.lower()


def test_failure_signature_masks_dynamic_path_segments():
    sig = build_failure_signature(
        page_scope="jobs.ashbyhq.com|/suno/9823587b-1ab4-4ecc-a6f1-32f744364bdc/application",
        classification="external_blocked",
        reason_code="anti_spam_or_risk_blocked",
        question_text="",
        action="click",
    )
    assert "{id}" in sig
    assert "external_blocked" in sig
