from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from openai import OpenAI
from playwright.sync_api import Page

LogFn = Callable[[str, str], None]


@dataclass
class ResumeMatchResult:
    selected_resume_path: Optional[str]
    score: int
    reason: str
    candidates_count: int
    jd_chars: int


def extract_jd_text_from_page(page: Page, max_chars: int = 12000) -> str:
    """
    Extract job description text from current page.
    当前阶段优先复用页面可见文本，后续可加更精细的 JD 区域定位规则。
    """
    try:
        text = page.inner_text("body")
    except Exception:
        return ""

    # 轻量清洗：压缩空白，避免给模型过多噪声
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    return cleaned[:max_chars]


def choose_best_resume_for_jd(
    jd_text: str,
    candidates: list[str],
    log_fn: Optional[LogFn] = None,
) -> ResumeMatchResult:
    """
    Choose best resume path for current JD.
    - 优先尝试 LLM 打分
    - 失败时回退到启发式评分
    """
    log = log_fn or (lambda _msg, _level="info": None)
    safe_candidates = [c for c in candidates if c]
    if not safe_candidates:
        return ResumeMatchResult(
            selected_resume_path=None,
            score=0,
            reason="no resume candidates",
            candidates_count=0,
            jd_chars=len(jd_text or ""),
        )

    # 单候选直接命中
    if len(safe_candidates) == 1:
        only = safe_candidates[0]
        return ResumeMatchResult(
            selected_resume_path=only,
            score=100,
            reason="single candidate only",
            candidates_count=1,
            jd_chars=len(jd_text or ""),
        )

    llm_result = _llm_score_resume_candidates(jd_text, safe_candidates, log)
    if llm_result is not None:
        return llm_result

    return _heuristic_score_resume_candidates(jd_text, safe_candidates)


def _llm_score_resume_candidates(
    jd_text: str,
    candidates: list[str],
    log: LogFn,
) -> Optional[ResumeMatchResult]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        log("resume matching: OPENAI_API_KEY missing, fallback to heuristic", "warn")
        return None

    if not jd_text or len(jd_text.strip()) < 120:
        log("resume matching: jd text too short, fallback to heuristic", "warn")
        return None

    model = os.getenv("RESUME_MATCH_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=api_key)
    candidate_lines = "\n".join(
        f"{idx + 1}. {Path(path).name}" for idx, path in enumerate(candidates)
    )

    system_prompt = (
        "You are a resume matching engine. "
        "Given a job description and candidate resume file names, "
        "select the most relevant resume index. "
        "Return strict JSON only."
    )
    user_prompt = f"""Job Description:
{jd_text[:8000]}

Candidate resumes:
{candidate_lines}

Return JSON only:
{{
  "best_index": <1-based index>,
  "score": <0-100>,
  "reason": "<brief reason>"
}}
"""
    try:
        completion = client.chat.completions.create(
            model=model,
            temperature=0.0,
            max_tokens=220,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = completion.choices[0].message.content or ""
        data = _safe_parse_json(raw)
        if not data:
            log("resume matching: llm json parse failed, fallback to heuristic", "warn")
            return None

        best_index = int(data.get("best_index", 0))
        score = int(data.get("score", 0))
        reason = str(data.get("reason", "")).strip() or "llm selected"
        if best_index < 1 or best_index > len(candidates):
            log(
                "resume matching: llm best_index out of range, fallback to heuristic",
                "warn",
            )
            return None

        chosen = candidates[best_index - 1]
        score = max(0, min(score, 100))
        return ResumeMatchResult(
            selected_resume_path=chosen,
            score=score,
            reason=reason,
            candidates_count=len(candidates),
            jd_chars=len(jd_text or ""),
        )
    except Exception as exc:
        log(f"resume matching: llm call failed ({exc}), fallback to heuristic", "warn")
        return None


def _heuristic_score_resume_candidates(
    jd_text: str,
    candidates: list[str],
) -> ResumeMatchResult:
    """
    Lightweight filename-token overlap scoring fallback.
    """
    jd_lower = (jd_text or "").lower()
    best_path = candidates[0]
    best_raw_score = -1

    for path in candidates:
        name = Path(path).name.lower()
        tokens = [t for t in re.split(r"[_\-\s\.]+", name) if len(t) > 1]
        overlap = sum(1 for t in tokens if t in jd_lower)
        if overlap > best_raw_score:
            best_raw_score = overlap
            best_path = path

    # 映射到 0-100 便于日志展示
    normalized = 55 + min(max(best_raw_score, 0) * 8, 40)
    return ResumeMatchResult(
        selected_resume_path=best_path,
        score=int(normalized),
        reason="heuristic filename keyword overlap",
        candidates_count=len(candidates),
        jd_chars=len(jd_text or ""),
    )


def _safe_parse_json(raw: str) -> dict | None:
    try:
        return json.loads(raw)
    except Exception:
        pass

    if "```" in raw:
        try:
            start = raw.find("```json")
            if start != -1:
                start = raw.find("\n", start) + 1
            else:
                start = raw.find("```") + 3
                start = raw.find("\n", start) + 1
            end = raw.find("```", start)
            if end != -1:
                return json.loads(raw[start:end].strip())
        except Exception:
            pass

    if "{" in raw and "}" in raw:
        try:
            start = raw.index("{")
            end = raw.rfind("}") + 1
            return json.loads(raw[start:end])
        except Exception:
            pass
    return None
