"""
语义反循环状态机工具（V2 拆分第二步）

职责：
- 生成稳定页面 scope
- 语义 guard 决策
- fail count 提升与动作结果记账
"""

from __future__ import annotations

from urllib.parse import urlsplit


def stable_page_scope(current_url: str) -> str:
    parsed = urlsplit(current_url or "")
    domain = (parsed.netloc or "unknown").lower()
    path = (parsed.path or "/").lower()
    stable_parts = [p for p in path.split("/") if p and p not in {"jobs", "job"}]
    normalized_path = "/" + "/".join(stable_parts[:3]) if stable_parts else "/"
    return f"{domain}{normalized_path}"


def semantic_loop_guard_decision(fail_count: int) -> str:
    if fail_count == 1:
        return "replan"
    if fail_count == 2:
        return "alternate"
    if fail_count >= 3:
        return "stop"
    return "none"


def promote_semantic_fail_count(semantic_fail_counts: dict[str, int], key: str) -> int:
    next_count = semantic_fail_counts.get(key, 0) + 1
    semantic_fail_counts[key] = next_count
    return next_count


def record_loop_action_result(
    *,
    action_fail_counts: dict[str, int],
    repeated_skip_counts: dict[str, int],
    semantic_fail_counts: dict[str, int],
    action_key: str,
    semantic_key: str,
    success: bool,
) -> None:
    if success:
        action_fail_counts[action_key] = 0
        repeated_skip_counts[action_key] = 0
        if semantic_key:
            semantic_fail_counts[semantic_key] = 0
        return
    action_fail_counts[action_key] = action_fail_counts.get(action_key, 0) + 1
    if semantic_key:
        semantic_fail_counts[semantic_key] = (
            semantic_fail_counts.get(semantic_key, 0) + 1
        )
