from autojobagent.core.terminal_guard import raw_response_implies_completion


def test_raw_response_implies_completion_true():
    raw = "Your application was successfully submitted. The process is complete."
    assert raw_response_implies_completion(raw) is True


def test_raw_response_implies_completion_false():
    raw = "I will now fill the location field and continue."
    assert raw_response_implies_completion(raw) is False
