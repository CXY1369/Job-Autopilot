from autojobagent.core.heuristics import detect_manual_required


def test_detect_manual_required_captcha():
    text = "Please complete the CAPTCHA to continue."
    assert detect_manual_required(text) is True


def test_detect_manual_required_login():
    text = "Sign in to continue. Enter your password or verification code."
    assert detect_manual_required(text) is True


def test_detect_manual_required_false():
    text = "Thank you for applying. Your application has been submitted."
    assert detect_manual_required(text) is False
