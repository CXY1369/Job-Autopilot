from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Optional

from playwright.sync_api import Page


@dataclass
class SimplifyConfig:
    enabled: bool = True
    complete_text: str = "Autofill complete!"
    timeout_ms: int = 25000
    poll_interval_ms: int = 400


@dataclass
class SimplifyResult:
    found: bool
    autofilled: bool
    message: str | None = None
    observations: list[str] | None = None


def run_simplify(page: Page, config: Optional[SimplifyConfig] = None) -> SimplifyResult:
    """
    轮询 Simplify 按钮，点击一次，等待完成信号。
    完成信号：
    - 任何 frame 中出现 “Autofill complete!”
    - 按钮文本变为 “Autofill this page again”
    """
    cfg = config or SimplifyConfig()
    if not cfg.enabled:
        return SimplifyResult(found=False, autofilled=False, message="Simplify disabled")

    deadline = time.time() + cfg.timeout_ms / 1000
    clicked = False
    last_seen = ""
    observations: list[str] = []
    clicked_handle = None

    def _frames(p: Page):
        yield p.main_frame
        for f in p.frames:
            yield f

    while time.time() < deadline:
        for frame in _frames(page):
            # 记录按钮文本
            try:
                btn = frame.get_by_text("Autofill this page", exact=False).first
                if btn and btn.is_visible():
                    last_seen = btn.inner_text().strip()
                    observations.append(f"see_btn:{last_seen}")
                    if not clicked:
                        btn.click()
                        clicked_handle = btn
                        observations.append(f"clicked:{last_seen}")
                        clicked = True
                        page.wait_for_timeout(700)
            except Exception as e:
                observations.append(f"btn_check_error:{type(e).__name__}")

            # 若已点击，尝试读取同一按钮的最新文本
            if clicked_handle:
                try:
                    txt_after = clicked_handle.inner_text().strip()
                    observations.append(f"clicked_btn_text:{txt_after}")
                    if "again" in txt_after.lower():
                        observations.append("clicked_btn_became_again")
                        return SimplifyResult(
                            found=True,
                            autofilled=True,
                            message="Clicked button text became again",
                            observations=observations,
                        )
                except Exception as e:
                    observations.append(f"clicked_btn_error:{type(e).__name__}")

            # 完成判定
            try:
                banner = frame.get_by_text("Autofill complete!", exact=False).first
                if banner and banner.is_visible():
                    observations.append("complete_banner_visible")
                    return SimplifyResult(
                        found=True,
                        autofilled=True,
                        message="Autofill complete banner",
                        observations=observations,
                    )
            except Exception as e:
                observations.append(f"banner_check_error:{type(e).__name__}")

            try:
                again_btn = frame.get_by_text("Autofill this page again", exact=False).first
                if again_btn and again_btn.is_visible():
                    observations.append("button_became_again")
                    return SimplifyResult(
                        found=True,
                        autofilled=True,
                        message="Button turned to again",
                        observations=observations,
                    )
            except Exception as e:
                observations.append(f"again_check_error:{type(e).__name__}")

            # 文本兜底（防止 get_by_text 未匹配到但文本已出现）
            try:
                body_text = frame.inner_text("body")
                if "Autofill complete!" in body_text:
                    observations.append("complete_text_found")
                    return SimplifyResult(
                        found=True,
                        autofilled=True,
                        message="Autofill complete text fallback",
                        observations=observations,
                    )
                if "Autofill this page again" in body_text:
                    observations.append("again_text_found")
                    return SimplifyResult(
                        found=True,
                        autofilled=True,
                        message="Button text fallback",
                        observations=observations,
                    )
            except Exception as e:
                observations.append(f"text_fallback_error:{type(e).__name__}")

        # 若本轮没有任何观察，尝试全局兜底点击与文本检查（避免遗漏 frame）
        try:
            btn_global = page.locator("text=Autofill this page").first
            if btn_global and btn_global.is_visible():
                observations.append("global_see_btn:Autofill this page")
                if not clicked:
                    btn_global.click()
                    clicked_handle = btn_global
                    observations.append("global_clicked:Autofill this page")
                    clicked = True
                    page.wait_for_timeout(700)
                try:
                    txt_after = btn_global.inner_text().strip()
                    observations.append(f"global_btn_text:{txt_after}")
                    if "again" in txt_after.lower():
                        observations.append("global_btn_became_again")
                        return SimplifyResult(
                            found=True,
                            autofilled=True,
                            message="Global button turned to again",
                            observations=observations,
                        )
                except Exception as e:
                    observations.append(f"global_btn_text_error:{type(e).__name__}")
        except Exception as e:
            observations.append(f"global_btn_error:{type(e).__name__}")

        try:
            body_global = page.inner_text("body")
            if "Autofill complete!" in body_global:
                observations.append("global_complete_text_found")
                return SimplifyResult(
                    found=True,
                    autofilled=True,
                    message="Global body complete text",
                    observations=observations,
                )
            if "Autofill this page again" in body_global:
                observations.append("global_again_text_found")
                return SimplifyResult(
                    found=True,
                    autofilled=True,
                    message="Global body again text",
                    observations=observations,
                )
        except Exception as e:
            observations.append(f"global_body_error:{type(e).__name__}")

        page.wait_for_timeout(cfg.poll_interval_ms)

    # 兜底：即便超时，也根据观测到的 again/complete 决定 autofilled
    if "button_became_again" in observations or "complete_banner_visible" in observations:
        return SimplifyResult(
            found=True,
            autofilled=True,
            message=f"Observed completion signal after timeout; last_seen_button={last_seen}",
            observations=observations,
        )
    if "clicked_btn_became_again" in observations:
        return SimplifyResult(
            found=True,
            autofilled=True,
            message=f"Clicked button turned to again after timeout; last_seen_button={last_seen}",
            observations=observations,
        )
    if clicked:
        return SimplifyResult(
            found=True,
            autofilled=False,
            message=f"Timeout waiting complete; last_seen_button={last_seen}",
            observations=observations,
        )
    if not observations:
        observations.append("no_observation")
    return SimplifyResult(
        found=False,
        autofilled=False,
        message="Simplify not found",
        observations=observations,
    )
