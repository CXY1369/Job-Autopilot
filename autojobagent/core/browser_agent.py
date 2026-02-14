"""
Browser-Use Agent 封装骨架。

未来这里将：
- 创建并管理 Browser-Use 会话
- 为每一个岗位执行「打开页面 → 识别 → 填写 → 点击 Next/Submit」的高层动作
- 封装对大模型的提示词与动作约束

当前仅提供高层接口定义，便于后续迭代。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..models.job_post import JobPost
from ..models.resume import Resume
from ..models.user_profile import UserProfile


@dataclass
class BrowserAgentConfig:
    max_retries_per_page: int = 3


class BrowserAgent:
    """
    对 Browser-Use 的高层封装。

    注意：当前不直接调用 browser_use 库，仅定义接口与后续实现的挂载点。
    """

    def __init__(self, config: Optional[BrowserAgentConfig] = None) -> None:
        self.config = config or BrowserAgentConfig()

    async def apply_on_job(
        self,
        job: JobPost,
        resume: Optional[Resume],
        user_profile: Optional[UserProfile],
        jd_text: Optional[str] = None,
    ) -> dict:
        """
        在一个岗位上执行完整的自动申请流程。

        返回值示例（后续可以定义为数据类）：
        {
            "success": bool,
            "manual_required": bool,
            "fail_reason": str | None,
            "manual_reason": str | None,
            "resume_used": str | None,
        }
        """
        # TODO:
        # - 创建 Browser-Use agent
        # - 设置系统提示词：目标、约束、安全边界
        # - 将 job/link + user_profile + resume 摘要 + jd_text 作为上下文输入
        # - 驱动浏览器完成多页申请流程

        return {
            "success": False,
            "manual_required": False,
            "fail_reason": "BrowserAgent not implemented yet",
            "manual_reason": None,
            "resume_used": resume.name if resume else None,
        }
