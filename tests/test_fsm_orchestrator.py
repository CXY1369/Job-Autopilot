from autojobagent.core.fsm_orchestrator import (
    decide_failure_recovery_path,
    decide_local_adjustment_path,
    decide_repeated_skip_path,
    decide_semantic_guard_path,
    derive_execution_phase,
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


def test_derive_execution_phase():
    assert (
        derive_execution_phase(
            state_status="done",
            has_next_action=False,
            action_is_progression=False,
            progression_blocked=False,
            manual_required=False,
            has_pending_macro_tasks=False,
            consecutive_failures=0,
        )
        == "finalize"
    )
    assert (
        derive_execution_phase(
            state_status="continue",
            has_next_action=True,
            action_is_progression=True,
            progression_blocked=False,
            manual_required=False,
            has_pending_macro_tasks=False,
            consecutive_failures=0,
        )
        == "submit"
    )
    assert (
        derive_execution_phase(
            state_status="continue",
            has_next_action=True,
            action_is_progression=False,
            progression_blocked=False,
            manual_required=False,
            has_pending_macro_tasks=True,
            consecutive_failures=0,
        )
        == "execute_chain"
    )
    assert (
        derive_execution_phase(
            state_status="continue",
            has_next_action=False,
            action_is_progression=False,
            progression_blocked=True,
            manual_required=False,
            has_pending_macro_tasks=False,
            consecutive_failures=0,
        )
        == "repair"
    )


def test_decide_local_adjustment_path():
    assert (
        decide_local_adjustment_path(
            action_success=True,
            is_macro_action=True,
            has_alternate_action=False,
            repeated_same_error=False,
            retry_count=0,
            retry_limit=3,
        )
        == "advance"
    )
    assert (
        decide_local_adjustment_path(
            action_success=False,
            is_macro_action=True,
            has_alternate_action=True,
            repeated_same_error=False,
            retry_count=1,
            retry_limit=3,
        )
        == "retry_same_task"
    )
    assert (
        decide_local_adjustment_path(
            action_success=False,
            is_macro_action=False,
            has_alternate_action=True,
            repeated_same_error=False,
            retry_count=1,
            retry_limit=3,
        )
        == "alternate_task"
    )
    assert (
        decide_local_adjustment_path(
            action_success=False,
            is_macro_action=False,
            has_alternate_action=False,
            repeated_same_error=True,
            retry_count=1,
            retry_limit=3,
        )
        == "repair_then_continue"
    )
    assert (
        decide_local_adjustment_path(
            action_success=False,
            is_macro_action=False,
            has_alternate_action=False,
            repeated_same_error=False,
            retry_count=3,
            retry_limit=3,
        )
        == "stop_manual"
    )
