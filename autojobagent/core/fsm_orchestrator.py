"""
运行状态机决策模块（V2 拆分第五步）

职责：
- 统一 run() 主循环中的关键分支决策
- 保持决策纯函数化，便于测试与回放
"""

from __future__ import annotations

from typing import Literal

SemanticGuardPath = Literal[
    "none",
    "replan",
    "alternate",
    "alternate_missing_replan",
    "stop",
]
RepeatedSkipPath = Literal["none", "alternate", "replan", "stop"]
FailureRecoveryPath = Literal[
    "none",
    "refresh",
    "stop_refresh_exhausted",
    "stop_max_failures",
]


def decide_semantic_guard_path(
    semantic_guard: str,
    *,
    has_alternate_action: bool,
) -> SemanticGuardPath:
    if semantic_guard == "replan":
        return "replan"
    if semantic_guard == "alternate":
        if has_alternate_action:
            return "alternate"
        return "alternate_missing_replan"
    if semantic_guard == "stop":
        return "stop"
    return "none"


def decide_repeated_skip_path(
    *,
    skip_count: int,
    has_alternate_action: bool,
) -> RepeatedSkipPath:
    if has_alternate_action:
        return "alternate"
    if skip_count == 1:
        return "replan"
    if skip_count >= 3:
        return "stop"
    return "none"


def decide_failure_recovery_path(
    *,
    consecutive_failures: int,
    max_consecutive_failures: int,
    refresh_attempts: int,
    max_refresh_attempts: int,
    refresh_exhausted: bool,
) -> FailureRecoveryPath:
    # 优先级与现有 run() 保持一致：
    # 1) 连续失败>=3 时先尝试刷新
    # 2) 刷新已耗尽则立即停机
    # 3) 最后再检查 max_consecutive_failures
    if consecutive_failures >= 3:
        if refresh_attempts < max_refresh_attempts:
            return "refresh"
        if refresh_exhausted:
            return "stop_refresh_exhausted"
    if consecutive_failures >= max_consecutive_failures:
        return "stop_max_failures"
    return "none"
