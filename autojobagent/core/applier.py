"""
自动投递执行模块。

流程：
1. 打开岗位链接
2. Simplify 自动填表（如果可用）
3. AI Agent 接管，像人类一样操作浏览器
4. 保存最终页面截图
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from ..db.database import SessionLocal
from ..config import list_upload_candidates
from ..models.job_log import JobLog
from ..models.job_post import JobPost
from .browser_manager import BrowserManager
from .debug_probe import append_debug_log
from .resume_matcher import extract_jd_text_from_page, choose_best_resume_for_jd
from .simplify_helper import probe_simplify_state, run_simplify
from .vision_agent import run_browser_agent


# 截图保存目录
SCREENSHOTS_DIR = Path(__file__).parent.parent / "storage" / "screenshots"


@dataclass
class ApplyResult:
    success: bool
    manual_required: bool = False
    fail_reason: Optional[str] = None
    manual_reason: Optional[str] = None
    resume_used: Optional[str] = None


def apply_for_job(job: JobPost) -> ApplyResult:
    """
    Playwright + Simplify + AI Agent 的单岗位执行流程。

    核心理念：让 AI 像人类一样操作浏览器，不写死逻辑。
    """
    _log(job.id, "=" * 50)
    _log(job.id, "🚀 开始自动投递")
    _log(job.id, f"   岗位: {job.title or '未命名'}")
    _log(job.id, f"   公司: {job.company or '未知'}")
    _log(job.id, f"   链接: {job.link}")
    _log(job.id, "=" * 50)

    session = None
    try:
        manager = BrowserManager(
            log_fn=lambda msg, level="info": _log(job.id, msg, level)
        )
        session = manager.launch()
        page = session.page

        # 1. 打开页面
        _log(job.id, "\n--- 步骤 1: 打开页面 ---")
        try:
            page.goto(job.link, wait_until="domcontentloaded", timeout=30000)
            _log(job.id, "✓ 页面加载成功")
        except PlaywrightTimeoutError:
            _log(job.id, "❌ 页面加载超时", "error")
            session.close()
            return ApplyResult(success=False, fail_reason="页面加载超时")

        # 等待页面稳定
        page.wait_for_timeout(2000)
        # region agent log
        append_debug_log(
            location="applier.py:post_goto",
            message="page snapshot before simplify",
            data={
                "job_id": job.id,
                "url": page.url,
                "form_count": _safe_count(page, "form"),
                "password_input_count": _safe_count(page, "input[type='password']"),
                "apply_button_count": _safe_count_by_text(page, "button, a", "apply"),
                "captcha_like_count": _safe_count(page, "[id*='captcha' i], [class*='captcha' i], iframe[src*='recaptcha']"),
            },
            run_id="pre-fix-debug",
            hypothesis_id="H3",
        )
        # endregion

        # 1.5 提取 JD + 匹配最佳简历（阶段A）
        _log(job.id, "\n--- 步骤 1.5: JD 提取与简历匹配 ---")
        jd_text = extract_jd_text_from_page(page)
        _log(job.id, f"JD 文本长度: {len(jd_text)} 字符")

        candidates = list_upload_candidates(max_files=50)
        _log(job.id, f"候选简历数量: {len(candidates)}")
        match = choose_best_resume_for_jd(
            jd_text=jd_text,
            candidates=candidates,
            log_fn=lambda msg, level="info": _log(job.id, msg, level),
        )
        if match.selected_resume_path:
            _persist_job_resume_used(job.id, match.selected_resume_path)
            job.resume_used = match.selected_resume_path
            _log(
                job.id,
                "匹配结果: "
                f"{Path(match.selected_resume_path).name} "
                f"(score={match.score}, reason={match.reason})",
            )
        else:
            _log(job.id, "未匹配到可用简历，后续上传将使用默认候选顺序", "warn")

        # 2. 预导航：非申请页时只负责进入申请页（不做填表/提交）
        if not _looks_like_application_page(page):
            _log(job.id, "ℹ 当前非申请页，先执行预导航进入申请页")
            # region agent log
            append_debug_log(
                location="applier.py:simplify_gate",
                message="skip simplify before application page",
                data={
                    "job_id": job.id,
                    "url": page.url,
                    "looks_like_application_page": _looks_like_application_page(page),
                    "form_count": _safe_count(page, "form"),
                },
                run_id="pre-fix-debug",
                hypothesis_id="H7",
            )
            # endregion
            _ = run_browser_agent(page, job, max_steps=8, pre_nav_only=True)

        # 2.5 到达申请页后优先检测/利用 Simplify
        simplify_applied = False
        if session.simplify_loaded and _looks_like_application_page(page):
            _log(job.id, "\n--- 步骤 2.5: 申请页 Simplify 状态检测 ---")
            simplify_state = probe_simplify_state(page)
            _log(
                job.id,
                f"ℹ Simplify 状态: {simplify_state.status} ({simplify_state.message or 'n/a'})",
            )
            # region agent log
            append_debug_log(
                location="applier.py:simplify_state_probe",
                message="simplify state probe on application page",
                data={
                    "job_id": job.id,
                    "url": page.url,
                    "simplify_state": simplify_state.status,
                    "simplify_message": simplify_state.message,
                    "observations": (simplify_state.observations or [])[:10],
                },
                run_id="pre-fix-debug",
                hypothesis_id="H7",
            )
            # endregion

            if simplify_state.status in ("ready", "running"):
                _log(job.id, "\n--- 步骤 2.6: Simplify 自动填表 ---")
                simplify_result = run_simplify(page)
                if simplify_result.autofilled:
                    _log(job.id, "✓ Simplify 填表完成（申请页）")
                    simplify_applied = True
                else:
                    _log(job.id, f"⚠ Simplify: {simplify_result.message}", "warn")
                page.wait_for_timeout(1000)
                # region agent log
                append_debug_log(
                    location="applier.py:post_simplify_after_navigation",
                    message="page snapshot after simplify on application page",
                    data={
                        "job_id": job.id,
                        "url": page.url,
                        "looks_like_application_page": _looks_like_application_page(page),
                        "form_count": _safe_count(page, "form"),
                        "password_input_count": _safe_count(page, "input[type='password']"),
                    },
                    run_id="pre-fix-debug",
                    hypothesis_id="H7",
                )
                # endregion
            elif simplify_state.status == "completed":
                simplify_applied = True
                _log(job.id, "✓ Simplify 已完成当前页自动填写")
            else:
                _log(job.id, "ℹ Simplify 当前不可用，交由 Agent 继续填写")

        # 3. AI Agent 接管（补全 + 提交）
        _log(job.id, "\n--- 步骤 3: AI Agent 智能操作 ---")
        _log(
            job.id, "AI Agent 已启用：上传白名单校验 + 前进门控（避免盲点 Next/Submit）"
        )
        if simplify_applied:
            _log(job.id, "ℹ 已完成 Simplify，AI Agent 将专注补全与提交")
        agent_success = run_browser_agent(page, job)  # 使用默认 max_steps=50

        # 4. 保存最终页面截图
        _log(job.id, "\n--- 保存最终页面截图 ---")
        screenshot_path = _save_final_screenshot(page, job.id)
        if screenshot_path:
            _log(job.id, f"✓ 截图已保存: {screenshot_path}")
        else:
            _log(job.id, "⚠ 截图保存失败", "warn")

        # 5. 结果处理
        _log(job.id, "\n--- 结果 ---")

        if agent_success:
            _log(job.id, "✓ 投递成功！")
            _log(job.id, "等待 5 秒后关闭页面...")
            try:
                page.wait_for_timeout(5000)
            except Exception:
                pass
            session.close()
            return ApplyResult(success=True, resume_used=job.resume_used)
        else:
            _log(job.id, "⚠ 投递可能未完成，需要人工检查", "warn")
            _log(job.id, "等待 5 秒后关闭页面...")
            try:
                page.wait_for_timeout(5000)
            except Exception:
                pass
            session.close()
            manual_reason = (
                getattr(job, "manual_reason_hint", None) or "AI Agent 未能完成全部操作"
            )
            return ApplyResult(
                success=False,
                manual_required=True,
                manual_reason=manual_reason,
                resume_used=job.resume_used,
            )

    except Exception as e:
        if session:
            try:
                session.close()
            except Exception:
                pass
        _log(job.id, f"❌ 投递过程异常: {e}", "error")
        return ApplyResult(
            success=False,
            fail_reason=str(e),
            resume_used=job.resume_used,
        )


def _log(job_id: int, message: str, level: str = "info") -> None:
    """写入日志"""
    with SessionLocal() as session:
        session.add(JobLog(job_id=job_id, level=level, message=message))
        session.commit()
    print(f"[job={job_id}] [{level.upper()}] {message}")


def _save_final_screenshot(page: Page, job_id: int) -> Optional[str]:
    """
    保存最终页面截图。

    Args:
        page: Playwright Page 对象
        job_id: 岗位 ID

    Returns:
        截图文件路径，失败则返回 None
    """
    try:
        # 确保目录存在
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

        # 生成文件名：job_{id}_{timestamp}.png
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"job_{job_id}_{timestamp}.png"
        filepath = SCREENSHOTS_DIR / filename

        # 保存全页截图
        page.screenshot(path=str(filepath), full_page=True)

        return str(filepath)
    except Exception as e:
        print(f"[job={job_id}] [ERROR] 截图保存失败: {e}")
        return None


def _persist_job_resume_used(job_id: int, resume_path: str) -> None:
    """
    将匹配出的简历路径持久化到 jobs.resume_used。
    """
    with SessionLocal() as session:
        db_job = session.get(JobPost, job_id)
        if not db_job:
            return
        db_job.resume_used = resume_path
        session.add(db_job)
        session.commit()


def _safe_count(page: Page, selector: str) -> int:
    try:
        return page.locator(selector).count()
    except Exception:
        return -1


def _safe_count_by_text(page: Page, selector: str, text_keyword: str) -> int:
    try:
        return page.locator(selector).filter(has_text=text_keyword).count()
    except Exception:
        return -1


def _looks_like_application_page(page: Page) -> bool:
    """轻量判断是否已进入申请页（用于控制 Simplify 执行时机）。"""
    try:
        current_url = (page.url or "").lower()
    except Exception:
        current_url = ""
    if "/application" in current_url or "/apply" in current_url:
        return True
    # URL 不可靠时用结构兜底：表单字段 + submit/apply 按钮同时出现
    form_fields = _safe_count(
        page, "input, textarea, select, [role='textbox'], [role='combobox'], [role='file_input']"
    )
    submit_like = _safe_count_by_text(page, "button, input[type='submit']", "submit")
    apply_like = _safe_count_by_text(page, "button, input[type='submit']", "apply")
    return form_fields >= 3 and (submit_like > 0 or apply_like > 0)
