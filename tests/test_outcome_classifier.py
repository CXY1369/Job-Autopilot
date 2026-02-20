from autojobagent.core.outcome_classifier import (
    assess_completion_confidence,
    classify_submission_outcome,
)


def test_assess_completion_confidence_positive():
    assessment = assess_completion_confidence(
        body_text="Thank you for applying. Your application has been submitted.",
        current_url="https://jobs.example.com/thanks",
        has_submit_button=False,
        has_error=False,
    )
    assert assessment.confirmed is True
    assert assessment.score >= 0.72


def test_assess_completion_confidence_rejects_external_blocked():
    assessment = assess_completion_confidence(
        body_text="We couldn't submit your application. It was flagged as possible spam.",
        current_url="https://jobs.example.com/application",
        has_submit_button=True,
        has_error=False,
    )
    assert assessment.confirmed is False
    assert assessment.signals["external_blocked"] is True


def test_classify_submission_outcome_prefers_validation_error_when_blocked_by_fields():
    outcome = classify_submission_outcome(
        evidence_text="Please complete required fields",
        action_success=False,
        progression_block_reason="missing required field",
        progression_block_snippets=["Location is required"],
    )
    assert outcome.classification == "validation_error"
