"""
语义意图推断模块（V2 拆分）

职责：
- label/text 意图推断
- 推断缓存 key 生成
- 快照 ref -> intents 映射
"""

from __future__ import annotations

import hashlib
import json

from .ui_snapshot import SnapshotItem


ALLOWED_LABEL_INTENTS = {
    "apply_entry",
    "login_action",
    "progression_action",
    "upload_request",
}
ALLOWED_TEXT_INTENTS = {"login_action", "upload_request"}


def intent_cache_key(labels: list[str], context: str = "") -> str:
    stable = "\n".join(sorted(labels))
    base = f"{stable}\n--ctx--\n{context[:600]}"
    return f"labels::{hashlib.sha1(base.encode('utf-8')).hexdigest()}"


def fallback_label_intents(label: str) -> set[str]:
    """当语义模型不可用时，使用极小硬规则集合兜底。"""
    text = (label or "").strip().lower()
    intents: set[str] = set()
    if not text:
        return intents

    if any(k in text for k in ["apply", "application", "candidature"]):
        intents.add("apply_entry")
        intents.add("progression_action")
    if any(k in text for k in ["next", "continue", "submit", "proceed", "review"]):
        intents.add("progression_action")
    if any(k in text for k in ["sign in", "log in", "login", "authenticate"]):
        intents.add("login_action")
    if any(k in text for k in ["upload", "attach", "resume", "cv", "file"]):
        intents.add("upload_request")
    return intents


def infer_snapshot_intents(
    snapshot_map: dict[str, SnapshotItem],
    visible_text: str,
    *,
    infer_label_intents_fn,
) -> dict[str, set[str]]:
    """为当前快照中的按钮/链接推断语义意图。"""
    ref_to_label: dict[str, str] = {}
    for ref, item in snapshot_map.items():
        if item.role not in ("button", "link"):
            continue
        name = (item.name or "").strip()
        if not name:
            continue
        ref_to_label[ref] = name

    if not ref_to_label:
        return {}

    label_intents = infer_label_intents_fn(
        list(ref_to_label.values()),
        context=visible_text[:800],
    )
    ref_intents: dict[str, set[str]] = {}
    for ref, label in ref_to_label.items():
        ref_intents[ref] = label_intents.get(label, set())
    return ref_intents


def infer_label_intents(
    labels: list[str],
    *,
    context: str = "",
    intent_cache: dict[str, dict[str, list[str]]],
    infer_label_intents_with_llm_fn,
) -> dict[str, set[str]]:
    """
    对一组 UI 文本做语义意图分类。
    优先使用低成本文本模型，失败回退到强共识关键词。
    """
    cleaned = []
    seen = set()
    for label in labels:
        text = (label or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    if not cleaned:
        return {}

    cache_key = intent_cache_key(cleaned, context)
    cached = intent_cache.get(cache_key)
    if cached is not None:
        return {k: set(v) for k, v in cached.items()}

    result = infer_label_intents_with_llm_fn(cleaned, context)
    if result is None:
        result = {label: fallback_label_intents(label) for label in cleaned}
    else:
        for label in cleaned:
            result.setdefault(label, fallback_label_intents(label))

    intent_cache[cache_key] = {k: sorted(v) for k, v in result.items()}
    return result


def infer_label_intents_with_llm(
    *,
    client,
    intent_model: str,
    labels: list[str],
    context: str,
    safe_parse_json_fn,
) -> dict[str, set[str]] | None:
    if not client:
        return None

    payload = [{"id": f"l{i + 1}", "text": text} for i, text in enumerate(labels[:40])]
    if not payload:
        return {}

    system_prompt = (
        "You classify browser UI label intents for job application automation. "
        "Return strict JSON only."
    )
    user_prompt = (
        "Classify each UI label into zero or more intents.\n"
        "Allowed intents:\n"
        "- apply_entry: enter/start job application\n"
        "- login_action: sign in/authenticate/account access\n"
        "- progression_action: next/continue/review/submit/proceed steps\n"
        "- upload_request: upload/attach file or resume\n"
        "Rules:\n"
        "1) Use semantic meaning, not literal keyword matching.\n"
        "2) Support variants and other languages.\n"
        "3) Be conservative; if uncertain, return empty intents for that label.\n"
        f"Page context (may help): {context[:600]}\n"
        f"Labels JSON:\n{json.dumps(payload, ensure_ascii=False)}\n"
        'Return JSON: {"items":[{"id":"l1","intents":["apply_entry"]}]}'
    )
    try:
        completion = client.chat.completions.create(
            model=intent_model,
            temperature=0.0,
            max_tokens=500,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = completion.choices[0].message.content or ""
        data = safe_parse_json_fn(raw)
        if not data or not isinstance(data.get("items"), list):
            return None
        id_to_text = {item["id"]: item["text"] for item in payload}
        out: dict[str, set[str]] = {v: set() for v in id_to_text.values()}
        for item in data["items"]:
            if not isinstance(item, dict):
                continue
            label_id = str(item.get("id", "")).strip()
            text = id_to_text.get(label_id)
            if not text:
                continue
            intents = item.get("intents", [])
            if not isinstance(intents, list):
                continue
            normalized = {str(x).strip() for x in intents}
            out[text].update(i for i in normalized if i in ALLOWED_LABEL_INTENTS)
        return out
    except Exception:
        return None


def infer_text_intents(
    text: str,
    *,
    limit: int = 1200,
    intent_cache: dict[str, dict[str, list[str]]],
    client=None,
    intent_model: str = "",
    safe_parse_json_fn=None,
) -> set[str]:
    """
    对整页文本做语义意图分类（低频、可缓存）。
    只输出少量全局意图。
    """
    snippet = (text or "").strip()
    if not snippet:
        return set()
    snippet = snippet[:limit]
    cache_key = f"text::{hashlib.sha1(snippet.encode('utf-8')).hexdigest()}"
    cached = intent_cache.get(cache_key)
    if cached is not None:
        intents = cached.get("__text__", [])
        return set(intents)

    intents: set[str] = set()
    if client and safe_parse_json_fn:
        try:
            completion = client.chat.completions.create(
                model=intent_model,
                temperature=0.0,
                max_tokens=220,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Classify page text intents for job application flow. "
                            "Return strict JSON."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Allowed intents: login_action, upload_request.\n"
                            "Use semantic meaning and multilingual understanding.\n"
                            f"Text:\n{snippet}\n"
                            'Return JSON: {"intents":["login_action"]}'
                        ),
                    },
                ],
            )
            raw = completion.choices[0].message.content or ""
            data = safe_parse_json_fn(raw)
            if data and isinstance(data.get("intents"), list):
                intents = {
                    str(x).strip()
                    for x in data["intents"]
                    if str(x).strip() in ALLOWED_TEXT_INTENTS
                }
        except Exception:
            intents = set()

    if not intents:
        lower = snippet.lower()
        if any(k in lower for k in ["upload", "attach", "resume", "cv"]):
            intents.add("upload_request")
        if any(k in lower for k in ["sign in", "log in", "login"]):
            intents.add("login_action")

    intent_cache[cache_key] = {"__text__": sorted(intents)}
    return intents
