"""
浏览器管理模块：统一管理 Playwright 浏览器启动、profile、扩展与事件日志。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import yaml
from playwright.sync_api import BrowserContext, Page, sync_playwright

LogFn = Callable[[str, str], None]


@dataclass
class BrowserSession:
    playwright: any
    context: BrowserContext
    page: Page
    simplify_loaded: bool

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            try:
                self.playwright.stop()
            except Exception:
                pass


class BrowserManager:
    """
    管理浏览器生命周期与配置，避免业务流程中重复拼装启动参数。
    """

    def __init__(self, log_fn: Optional[LogFn] = None) -> None:
        self._log = log_fn or (lambda msg, level="info": None)
        self._settings = self._load_settings()

    def launch(self) -> BrowserSession:
        """启动持久化浏览器并返回会话。"""
        browser_cfg = self._settings.get("browser", {})
        simplify_cfg = self._settings.get("simplify", {})

        headless = bool(browser_cfg.get("headless", False))
        slow_mo = int(browser_cfg.get("slow_mo", 0))
        raw_profile_dir = browser_cfg.get("user_data_dir") or "~/.cache/autojobagent/chrome-profile"
        user_data_dir = str(Path(raw_profile_dir).expanduser())
        executable_path = browser_cfg.get("executable_path")

        simplify_enabled = bool(simplify_cfg.get("enabled", True))
        simplify_path = None
        if simplify_enabled:
            raw_simplify_path = browser_cfg.get("simplify_extension_path") or ""
            simplify_path = (
                str(Path(raw_simplify_path).expanduser())
                if raw_simplify_path
                else self._resolve_simplify_extension_path()
            )

        launch_args = {
            "headless": headless,
            "slow_mo": slow_mo if slow_mo > 0 else None,
            "user_data_dir": user_data_dir,
            "args": [],
        }
        if executable_path:
            launch_args["executable_path"] = executable_path

        if simplify_path:
            launch_args["args"].extend(
                [
                    f"--disable-extensions-except={simplify_path}",
                    f"--load-extension={simplify_path}",
                ]
            )
            self._log("✓ 已加载 Simplify 扩展")
        else:
            if simplify_enabled:
                self._log("⚠ 未找到 Simplify 扩展", "warn")

        # 清理 None 参数
        launch_args = {k: v for k, v in launch_args.items() if v is not None}

        playwright = sync_playwright().start()
        context = playwright.chromium.launch_persistent_context(**launch_args)
        page = context.new_page()

        self._attach_basic_listeners(page)
        self._attach_context_listeners(context)

        return BrowserSession(
            playwright=playwright,
            context=context,
            page=page,
            simplify_loaded=bool(simplify_path),
        )

    def _attach_basic_listeners(self, page: Page) -> None:
        """采集页面基础错误信息，写入日志便于排查。"""
        try:
            page.on(
                "console",
                lambda msg: self._log(f"[console:{msg.type}] {msg.text}", "warn")
                if msg.type in ("error", "warning")
                else None,
            )
            page.on(
                "pageerror",
                lambda exc: self._log(f"[pageerror] {exc}", "error"),
            )
        except Exception:
            pass

    def _attach_context_listeners(self, context: BrowserContext) -> None:
        try:
            context.on(
                "requestfailed",
                lambda req: self._log(
                    f"[requestfailed] {req.method} {req.url}", "warn"
                ),
            )
        except Exception:
            pass

    def _load_settings(self) -> dict:
        """读取项目配置文件（autojobagent/config.yaml）。"""
        config_path = Path(__file__).parent.parent / "config.yaml"
        if not config_path.exists():
            return {}
        try:
            return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    def _resolve_simplify_extension_path(self) -> Optional[str]:
        """查找 Simplify 扩展路径（macOS Chrome 默认路径）。"""
        base = Path(
            "~/Library/Application Support/Google/Chrome/Default/Extensions"
        ).expanduser()
        extension_id = "pbanhockgagggenencehbnadejlgchfc"
        target_dir = base / extension_id
        if not target_dir.exists():
            return None
        versions = sorted(target_dir.iterdir(), reverse=True)
        for v in versions:
            if v.is_dir():
                return str(v)
        return None
