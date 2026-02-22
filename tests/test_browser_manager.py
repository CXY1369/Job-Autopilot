from autojobagent.core.browser_manager import BrowserManager


def test_ignore_preload_unused_warning():
    manager = BrowserManager()
    text = (
        "The resource https://app.ashbyhq.com/api/images/org-theme-logo/foo.png "
        "was preloaded using link preload but not used within a few seconds from "
        "the window's load event. Please make sure it has an appropriate `as` value."
    )
    assert manager._should_ignore_console_warning("warning", text) is True


def test_do_not_ignore_other_warnings():
    manager = BrowserManager()
    assert (
        manager._should_ignore_console_warning(
            "warning",
            "React hydration warning: text content does not match server-rendered HTML.",
        )
        is False
    )
    assert (
        manager._should_ignore_console_warning(
            "error",
            "The resource was preloaded using link preload but not used.",
        )
        is False
    )


def test_ignore_chrome_extension_csp_noise():
    manager = BrowserManager()
    text = (
        "Refused to load the script 'chrome-extension://abc/js/pageScript.bundle.js' "
        "because it violates the following Content Security Policy directive."
    )
    assert manager._should_ignore_console_warning("error", text) is True


def test_ignore_requestfailed_for_extension_url():
    events: list[tuple[str, str]] = []
    manager = BrowserManager(log_fn=lambda msg, lvl="info": events.append((lvl, msg)))

    class _Req:
        method = "GET"
        url = "chrome-extension://abc/static.js"

    manager._handle_request_failed(_Req())
    assert events == []
