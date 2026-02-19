"""
规划辅助模块（V2 拆分第三步）

职责：
- LLM JSON 安全解析
- 对 Simplify/Assist 相关文案做去幻觉清洗
"""

from __future__ import annotations

import json


def safe_parse_json(raw: str) -> dict | None:
    """安全解析 JSON，支持 markdown 代码块包装。"""
    try:
        return json.loads(raw)
    except Exception:
        pass

    if "```" in raw:
        try:
            start = raw.find("```json")
            if start != -1:
                start = raw.find("\n", start) + 1
                end = raw.find("```", start)
                if end != -1:
                    return json.loads(raw[start:end].strip())
            start = raw.find("```")
            if start != -1:
                start = raw.find("\n", start) + 1
                end = raw.find("```", start)
                if end != -1:
                    return json.loads(raw[start:end].strip())
        except Exception:
            pass

    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start : end + 1])
    except Exception:
        pass
    return None


def sanitize_simplify_claims(text: str | None) -> str | None:
    if not text:
        return text
    lowered = text.lower()
    if "simplify" not in lowered:
        return text
    claim_markers = [
        "已自动填写",
        "自动填写完成",
        "simplify 已",
        "simplify已",
        "autofill complete",
        "autofilled",
    ]
    if any(marker in lowered for marker in claim_markers):
        return text.replace("Simplify", "页面").replace("simplify", "页面")
    return text
