from autojobagent.core.heuristics import assess_manual_required, detect_manual_required


def test_detect_manual_required_captcha():
    text = "Please complete the CAPTCHA to continue."
    assert detect_manual_required(text) is True


def test_detect_manual_required_login():
    text = "Sign in to continue. Enter your password or verification code."
    assert detect_manual_required(text) is True


def test_detect_manual_required_false():
    text = "Thank you for applying. Your application has been submitted."
    assert detect_manual_required(text) is False


def test_assess_manual_required_apply_entry_not_login():
    text = (
        "Sign in to save this job. You can still apply now for this position without login."
    )
    result = assess_manual_required(
        text,
        password_input_count=0,
        captcha_element_count=0,
        has_login_button=True,
        has_apply_cta=True,
    )
    assert result.manual_required is False
    assert result.reason == "apply_entry_page_not_login"


def test_assess_manual_required_login_form_with_password():
    text = "Please sign in and continue."
    result = assess_manual_required(
        text,
        password_input_count=1,
        captcha_element_count=0,
        has_login_button=True,
        has_apply_cta=False,
    )
    assert result.manual_required is True
    assert result.reason == "login_form_detected"


def test_assess_manual_required_captcha_dom_signal():
    text = "Continue to application."
    result = assess_manual_required(
        text,
        password_input_count=0,
        captcha_element_count=1,
        has_login_button=False,
        has_apply_cta=False,
    )
    assert result.manual_required is True
    assert result.reason == "captcha_detected"
