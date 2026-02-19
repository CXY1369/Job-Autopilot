from autojobagent.core.fsm_orchestrator import (
    decide_failure_recovery_path,
    decide_repeated_skip_path,
    decide_semantic_guard_path,
)


def test_decide_semantic_guard_path():
    assert decide_semantic_guard_path("none", has_alternate_action=False) == "none"
    assert decide_semantic_guard_path("replan", has_alternate_action=False) == "replan"
    assert (
        decide_semantic_guard_path("alternate", has_alternate_action=True)
        == "alternate"
    )
    assert (
        decide_semantic_guard_path("alternate", has_alternate_action=False)
        == "alternate_missing_replan"
    )
    assert decide_semantic_guard_path("stop", has_alternate_action=False) == "stop"


def test_decide_repeated_skip_path():
    assert (
        decide_repeated_skip_path(skip_count=1, has_alternate_action=True)
        == "alternate"
    )
    assert (
        decide_repeated_skip_path(skip_count=1, has_alternate_action=False) == "replan"
    )
    assert decide_repeated_skip_path(skip_count=2, has_alternate_action=False) == "none"
    assert decide_repeated_skip_path(skip_count=3, has_alternate_action=False) == "stop"


def test_decide_failure_recovery_path_priority():
    # 连续失败>=3 且还能刷新：优先 refresh
    assert (
        decide_failure_recovery_path(
            consecutive_failures=5,
            max_consecutive_failures=5,
            refresh_attempts=0,
            max_refresh_attempts=2,
            refresh_exhausted=False,
        )
        == "refresh"
    )
    # 刷新已耗尽：直接停机
    assert (
        decide_failure_recovery_path(
            consecutive_failures=4,
            max_consecutive_failures=5,
            refresh_attempts=2,
            max_refresh_attempts=2,
            refresh_exhausted=True,
        )
        == "stop_refresh_exhausted"
    )
    # 没有 refresh 路径时按 max fail 停机
    assert (
        decide_failure_recovery_path(
            consecutive_failures=5,
            max_consecutive_failures=5,
            refresh_attempts=2,
            max_refresh_attempts=2,
            refresh_exhausted=False,
        )
        == "stop_max_failures"
    )
    assert (
        decide_failure_recovery_path(
            consecutive_failures=2,
            max_consecutive_failures=5,
            refresh_attempts=0,
            max_refresh_attempts=2,
            refresh_exhausted=False,
        )
        == "none"
    )
