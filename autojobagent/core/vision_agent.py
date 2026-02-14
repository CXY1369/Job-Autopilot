"""
视觉 AI Agent：像人类一样操作浏览器。

核心循环：
1. 观察（截图）
2. 思考（LLM 分析当前状态，决定下一步）
3. 行动（执行单个操作）
4. 反馈（检查结果，继续循环）

不写死逻辑，让 LLM 动态决策。
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal

from openai import OpenAI
from playwright.sync_api import Page
from PIL import Image

from ..db.database import SessionLocal
from ..models.job_log import JobLog
from ..config import (
    get_user_info_for_prompt,
    load_agent_guidelines,
    list_upload_candidates,
    is_upload_path_allowed,
    resolve_upload_candidate,
)
from .browser_manager import BrowserManager
from .ui_snapshot import build_ui_snapshot, SnapshotItem
from .heuristics import detect_manual_required


# 截图保存目录
STORAGE_DIR = Path(__file__).parent.parent / "storage" / "screenshots"
# Debug log 目录/路径（NDJSON）
DEBUG_LOG_DIR = Path(__file__).parent.parent / "storage" / "logs"
TRACE_DIR = Path(__file__).parent.parent / "storage" / "logs"
DEBUG_LOG_PATH = DEBUG_LOG_DIR / "vision_agent.ndjson"


DEFAULT_FALLBACK_MODELS = [
    "gpt-4o",           # 默认模型：最佳视觉理解
    "gpt-4o-2024-11-20", # 最新版本
    "gpt-4.1",          # 新一代模型
    "gpt-4.1-mini",     # 轻量版
    "gpt-5-mini",       # 实验版
    "gpt-4-turbo",      # 稳定后备
    "gpt-4o-mini",      # 最后备选
]

# 截图压缩配置
SCREENSHOT_MAX_WIDTH = 1280  # 最大宽度（像素）
SCREENSHOT_JPEG_QUALITY = 75  # JPEG 质量（0-100），75 是清晰度和体积的良好平衡


@dataclass
class AgentAction:
    """单个操作"""
    action: str  # click, fill, type, select, upload, scroll, wait, done, stuck
    ref: Optional[str] = None  # 目标元素 ref（优先）
    selector: Optional[str] = None  # 目标元素的文本/描述
    value: Optional[str] = None  # 填入的值
    element_type: Optional[str] = None  # 元素类型：button, link, checkbox, radio, input, option, text
    reason: Optional[str] = None  # 为什么这样做


@dataclass
class AgentState:
    """Agent 当前状态"""
    status: Literal["continue", "done", "stuck", "error"]
    summary: str  # 当前页面状态描述
    next_action: Optional[AgentAction] = None
    raw_response: Optional[str] = None


class BrowserAgent:
    """
    像人类一样操作浏览器的 AI Agent。
    
    核心能力：
    - 观察：截图 + 获取页面文本
    - 思考：让 LLM 分析状态并决定下一步
    - 行动：执行点击、填写、滚动等基本操作
    - 循环：不断重复直到任务完成或放弃
    """
    
    def __init__(self, page: Page, job, max_steps: int = 50):
        self.page = page
        self.job = job
        self.job_id = job.id
        self.max_steps = max_steps
        self.step_count = 0
        self.history: list[str] = []  # 操作历史，帮助 LLM 避免重复
        
        # OpenAI 客户端
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=self.api_key) if self.api_key else None
        
        settings = BrowserManager()._load_settings()
        self.llm_cfg = settings.get("llm", {})
        fallback_models = self.llm_cfg.get("fallback_models") or DEFAULT_FALLBACK_MODELS
        if not isinstance(fallback_models, list) or not fallback_models:
            fallback_models = DEFAULT_FALLBACK_MODELS
        preferred_model = self.llm_cfg.get("model")
        if preferred_model and preferred_model in fallback_models:
            fallback_models = [preferred_model] + [m for m in fallback_models if m != preferred_model]
        self.fallback_models = fallback_models
        # 默认首选 GPT-4o
        self.model_index = 0
        self.model = self.fallback_models[self.model_index]
        
        # 创建 job 专属截图目录
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.screenshot_dir = STORAGE_DIR / f"job_{self.job_id}_{timestamp}"
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._last_screenshot_bytes: bytes = b""  # 缓存最近一次截图用于保存
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        self.trace_path = TRACE_DIR / f"agent_trace_job_{self.job_id}_{timestamp}.ndjson"
        
        # 智能终止机制
        self.consecutive_failures = 0  # 连续失败计数
        self.max_consecutive_failures = 5  # 连续失败阈值
        self.last_url = None  # 页面 URL 跟踪（用于检测页面跳转）
        self._last_snapshot_map: dict[str, SnapshotItem] = {}
        self.upload_candidates: list[str] = list_upload_candidates(max_files=30)
        self.preferred_resume_path: str | None = getattr(job, "resume_used", None)
        self._last_upload_signals: list[str] = []

    #region agent log
    def _ndjson_log(self, hypothesis_id: str, location: str, message: str, data: dict):
        """轻量级调试日志，写入 NDJSON 文件。"""
        payload = {
            "sessionId": "debug-session",
            "runId": "run1",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        try:
            DEBUG_LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass
    #endregion
    
    def run(self) -> bool:
        """
        运行 Agent 主循环，返回是否成功完成任务。
        """
        self._log("========== AI Agent 开始运行 ==========")
        self._log(f"最大步数: {self.max_steps}")
        
        if not self.client:
            self._log("❌ OPENAI_API_KEY 未设置，无法运行 Agent", "error")
            return False
        
        while self.step_count < self.max_steps:
            self.step_count += 1
            self._log(f"\n--- 第 {self.step_count} 步 ---")
            
            # 1. 观察
            state = self._observe_and_think()
            
            if state.status == "error":
                self._log(f"❌ 观察/思考出错: {state.summary}", "error")
                continue
            
            # 2. 记录 LLM 的分析
            self._log(f"📋 状态: {state.summary}")
            
            # 3. 检查是否完成（带二次验证）
            if state.status == "done":
                self._log("🔍 Agent 判断任务完成，进行二次验证...")
                
                # 二次验证：检查页面是否真的显示成功信息
                is_really_done, verification_msg = self._verify_completion()
                
                if is_really_done:
                    self._log(f"✓ 二次验证通过: {verification_msg}")
                    self._log("========== AI Agent 运行结束 ==========")
                    return True
                else:
                    self._log(f"⚠ 二次验证失败: {verification_msg}", "warn")
                    self._log("   继续执行，可能还有未完成的步骤...")
                    # 不返回，继续循环
                    continue
            
            if state.status == "stuck":
                self._log("⚠ Agent 判断无法继续，需要人工介入", "warn")
                self._log("========== AI Agent 运行结束 ==========")
                return False
            
            # 4. 执行下一步操作
            if state.next_action:
                action = state.next_action
                elem_info = f"[{action.element_type}]" if action.element_type else ""
                ref_info = f"(ref={action.ref}) " if action.ref else ""
                self._log(f"🎯 计划: {action.action} {ref_info}{elem_info} {action.selector or ''} {action.value or ''}")
                if action.reason:
                    self._log(f"   原因: {action.reason}")
                
                success = self._execute_action(action)
                
                # 记录到历史（让 AI 能看到操作结果，从而调整策略）
                target_desc = action.ref or (action.selector or "")
                action_desc = f"{action.action}({target_desc}"
                if action.value:
                    action_desc += f", {action.value}"
                action_desc += ")"
                
                if success:
                    self.history.append(f"步骤{self.step_count}: {action_desc} ✓ [请检查截图确认是否正确生效]")
                    self.consecutive_failures = 0  # 重置连续失败计数
                else:
                    self.history.append(f"步骤{self.step_count}: {action_desc} ✗失败 [操作未成功，可能需要换方法]")
                    self.consecutive_failures += 1  # 增加连续失败计数
                
                if success:
                    self._log("   ✓ 执行成功")
                else:
                    self._log(f"   ❌ 执行失败 (连续失败: {self.consecutive_failures}/{self.max_consecutive_failures})", "warn")
                    # 保存失败截图（带 _failed 后缀）
                    try:
                        failed_screenshot = self.page.screenshot(full_page=True)
                        failed_compressed = self._compress_screenshot(failed_screenshot)
                        failed_path = self.screenshot_dir / f"step_{self.step_count:02d}_failed.jpg"
                        failed_path.write_bytes(failed_compressed)
                        self._log(f"   💾 失败截图: {failed_path.name}")
                    except Exception:
                        pass
                    
                    # 智能终止：连续失败次数过多
                    if self.consecutive_failures >= self.max_consecutive_failures:
                        self._log(f"⚠ 连续 {self.consecutive_failures} 次操作失败，停止执行", "warn")
                        self._log("========== AI Agent 运行结束（智能终止）==========")
                        return False
                
                # 等待页面响应后立即截图（让 AI 看到实时变化）
                # 短暂等待让页面 UI 更新（如下拉框出现）
                self.page.wait_for_timeout(500)
            else:
                self._log("⚠ LLM 没有给出下一步操作", "warn")
        
        self._log(f"⚠ 已达到最大步数 {self.max_steps}，停止执行", "warn")
        self._log("========== AI Agent 运行结束 ==========")
        return False
    
    def _observe_and_think(self) -> AgentState:
        """
        观察当前页面状态，让 LLM 思考下一步。
        """
        # 1. 截图（压缩优化）
        screenshot_b64 = None
        try:
            png_bytes = self.page.screenshot(full_page=True)
            original_size = len(png_bytes) / 1024
            
            # 压缩截图：PNG → JPEG，并限制宽度
            compressed_bytes = self._compress_screenshot(png_bytes)
            screenshot_b64 = base64.b64encode(compressed_bytes).decode("utf-8")
            compressed_size = len(compressed_bytes) / 1024
            
            # 保存截图到 job 专属目录
            self._last_screenshot_bytes = compressed_bytes
            screenshot_path = self.screenshot_dir / f"step_{self.step_count:02d}.jpg"
            screenshot_path.write_bytes(compressed_bytes)
            
            ratio = (1 - compressed_size / original_size) * 100 if original_size > 0 else 0
            self._log(f"📸 截图成功: {original_size:.1f} KB → {compressed_size:.1f} KB (压缩 {ratio:.0f}%)")
            self._log(f"   💾 已保存: {screenshot_path.name}")
        except Exception as e:
            self._log(f"❌ 截图失败: {e}", "error")
            return AgentState(status="error", summary=f"截图失败: {e}")
        
        # 2. 获取页面文本
        try:
            visible_text = self.page.inner_text("body")[:5000]
        except Exception:
            visible_text = ""

        # 2.5 生成可交互元素快照
        snapshot_text, snapshot_map = build_ui_snapshot(self.page)
        self._last_snapshot_map = snapshot_map

        # 2.6 检测登录/验证码等需人工介入的场景
        if detect_manual_required(visible_text):
            self._step_log(
                event="manual_required",
                payload={"reason": "login_or_captcha"},
            )
            return AgentState(
                status="stuck",
                summary="检测到登录/验证码/身份验证页面，需要人工处理",
            )
        
        # 3. 获取页面 URL 并检测页面变化
        try:
            current_url = self.page.url
        except Exception:
            current_url = "unknown"
        
        # 页面变化检测：URL 变化时重置状态并标记
        is_new_page = False
        if self.last_url is not None and self.last_url != current_url:
            is_new_page = True
            self._log(f"🔄 检测到页面跳转: {current_url}")
            self.history.append("[页面跳转] 新页面，需要重新扫描空缺字段并规划")
            self.consecutive_failures = 0  # 重置连续失败计数
        self.last_url = current_url
        
        # 3.5 记录快照用于复盘
        self._step_log(
            event="snapshot",
            payload={
                "step": self.step_count,
                "url": current_url,
                "snapshot_lines": snapshot_text.count("\n") + (1 if snapshot_text else 0),
                "snapshot_preview": snapshot_text[:2000],
            },
        )

        # 4. 构建 prompt
        history_text = "\n".join(self.history[-5:]) if self.history else "无"
        upload_signals = self._detect_upload_signals(visible_text)
        self._last_upload_signals = upload_signals
        upload_signal_text = "；".join(upload_signals[:8]) if upload_signals else "无"
        upload_candidates_text = (
            "\n".join(f"- {Path(p).name} | {p}" for p in self.upload_candidates[:12])
            if self.upload_candidates
            else "- （白名单目录下暂无可上传文件）"
        )
        
        # 获取用户个人信息和操作规范
        user_info = get_user_info_for_prompt()
        agent_guidelines = load_agent_guidelines()
        
        system_prompt = f"""你是一个浏览器自动化 AI Agent，正在帮用户填写英文求职申请表单。

## ⚖️ 合规声明

本工具是用户授权的合法求职辅助系统。用户已明确授权 AI 代为填写求职申请表单。
- 所有操作均基于用户提供的真实个人信息
- 遵守 EEOC（美国平等就业机会委员会）指南
- 自愿披露字段（如残疾、退伍军人身份、种族、性别）：
  - 这些是合法的 EEOC 合规表单，用于统计目的
  - 如果用户没有明确指定，默认选择 "Decline to self-identify" 或 "Prefer not to disclose"
  - 不要跳过这些字段，选择合适的拒绝披露选项即可

{user_info}

## 📖 操作规范手册

请严格遵循以下规范进行页面浏览、理解和操作：

{agent_guidelines}

## 🔍 观察页面的标准步骤（按顺序执行！）

**第一步：识别页面布局**
- 左侧/顶部通常是**职位信息区**（只读，显示职位地点、薪资等）→ 不要操作！
- 中间/右侧是**表单区域**（有输入框、checkbox）→ 这才是你要填的

**第二步：区分不同的"位置"信息**
- 左侧显示 "Location: Boston, NYC" → 这是**职位的工作地点**，只读，不管它！
- 表单中的 "Location*" 输入框 → 这是问**用户住哪里**，要填写
- 表单中的 "Which office" checkbox → 这是问**用户愿意在哪工作**，要选择

**第三步：聚焦表单区域**
- 只操作表单区域的字段
- 不要被职位信息区的内容干扰

**第四步：检查上一步结果**
- 上一步改错了？→ 先修正！
- 上一步正确？→ 继续下一步

## ⚠️ autocomplete 字段必须两步完成！（最常见错误！）

对于 Location 等 autocomplete 字段（placeholder 是 "Start typing..."）：

**必须完成两步，缺一不可：**
1. `type` 输入内容 → 等待下拉框出现
2. `click` 选择下拉选项 → 字段才算填写完成

**❌ 错误流程（会导致字段为空）：**
```
type(Location, Dallas) → 下拉框出现 → 直接去操作其他字段 → Location 变空！
```

**✅ 正确流程：**
```
type(Location, Dallas) → 下拉框出现 → click(Dallas, Texas, United States) → 完成！
```

**🔍 关键判断规则：**
| 你看到什么 | 下一步必须做什么 |
|-----------|-----------------|
| 下拉框出现，有选项列表 | **必须 click 选择选项！不能跳过！** |
| autocomplete 字段显示 "Start typing..." | 需要 type 输入 |
| autocomplete 字段显示完整地址（如 "Dallas, Texas, United States"） | 已完成，可以跳过 |

**⚠️ 绝对禁止：在下拉框出现时去操作其他字段或点击 Submit！**

## checkbox 多选逻辑（重要！）

**取交集原则：**
1. 查看页面提供的所有选项
2. 对比用户偏好（从用户信息中获取）
3. 交集 = 用户偏好中有的 AND 页面也提供的

**模糊匹配：**
- Boston = Boston (Cambridge) ✓
- New York = New York City (Chelsea) = NYC ✓
- SF = San Francisco ✓
- 推理判断是同一事物 → 使用**页面显示的完整名称**

**示例：**
```
用户偏好: [Boston, New York, SF, LA, Dallas]
页面选项: [Boston (Cambridge), NYC (Chelsea), LA (Venice), SF, Remote only]
交集: Boston (Cambridge), NYC (Chelsea), LA (Venice), SF
→ 排除 Remote only（用户偏好里没有）
```

**全部执行规则：**
- 交集有 N 个选项，就必须勾选 N 个
- 规划了选 4 个城市 → 全部勾选后再继续
- 不要选一个就认为完成！

## 开放式问题处理

当页面只有问题没有选项（如"你的技能是什么？"）：
- 从用户资料提取相关信息
- 默认填写 3 个有效值
- 用逗号分隔
- 示例：fill("Python, Machine Learning, Deep Learning")

## 观察当前截图并决定操作

- **下拉框出现** → **立即点击正确选项**（最高优先级！）
- **空的必填字段** → 填写内容
- **checkbox 多选** → 按交集规划**逐个勾选**，全部完成再继续
- **Submit 按钮且没有错误提示** → 点击提交
- **感谢信息** → 返回 done

## 可用操作

| 操作 | 使用场景 | selector/ref | value |
|------|----------|--------------|-------|
| click | 按钮、Yes/No选项、checkbox、radio、下拉选项 | 元素文本或 ref | - |
| fill | 普通输入框（Name、Email等） | 字段标签或 ref | 内容 |
| type | autocomplete 输入框（Location等） | 字段标签或 ref | 内容 |
| upload | 上传简历/附件（仅在页面有上传信号时） | 上传控件文本或 ref | 候选文件名或完整路径 |
| scroll | 滚动页面 | - | up/down |
| done | 任务完成 | - | - |
| stuck | 无法继续 | - | - |

**重要区分：**
- Yes/No 按钮 → 用 **click**，selector 填 "Yes" 或 "No"
- 文本输入框 → 用 fill 或 type
- 看到 "Start typing..." → 用 type

## 返回 JSON（优先使用 ref）
{{
  "status": "continue/done/stuck",
  "summary": "当前看到什么（中文）",
  "next_action": {{
    "action": "操作",
    "ref": "可交互元素 ref（优先使用）",
    "element_type": "button/link/checkbox/radio/input/option",
    "selector": "目标",
    "value": "值",
    "reason": "为什么"
  }}
}}

## 规则
1. 使用用户真实信息，不编造
2. 所有内容用英文填写
3. 已上传的文件不重复上传
4. 只有在页面存在上传信号时才允许使用 upload 动作

## 什么时候返回 stuck？（重要！不要轻易放弃！）

**只有这些情况才返回 stuck：**
- 需要登录但没有账号
- 出现验证码（CAPTCHA）
- 页面完全无法加载
- 需要付费

**这些情况不是 stuck，要继续操作：**
- 某个字段填错了 → 点击正确选项修复
- checkbox 选错了 → 点击正确的 checkbox
- 有错误提示 → 修复对应字段
- 页面有多个选项 → 选择最合适的

**核心原则：能操作就操作，不要轻易放弃！**"""

        # 构建 user_prompt（根据是否是新页面调整引导）
        new_page_hint = "[新页面] " if is_new_page else ""
        
        user_prompt = f"""历史:
{history_text}

## 页面可见文本（截断）
{visible_text}

## 可交互元素快照（ref → 元素）
{snapshot_text}

## 上传信号检测
{upload_signal_text}

## 白名单可上传候选文件（仅可从以下文件中选择）
{upload_candidates_text}

## {new_page_hint}请按以下步骤处理当前页面：

**1. 完整扫描并规划（列出所有空缺！）**
- Simplify 是否已自动填写完成？
- 列出**所有**空缺必填字段，不要只说第一个！
- 每个字段给出**具体值**（从用户信息查找）
- checkbox 多选：取"用户偏好 ∩ 页面选项"的交集（模糊匹配）
- 开放式问题（无选项）：默认填 3 个相关值
- 示例：" 空缺 3 项：1. Location → Dallas；2. Which office → 交集4个(Boston/NYC/LA/SF)；3. Skills → Python, ML, DL"

**规则：规划的选项必须全部执行！**
- checkbox 规划了 4 个 → 选完 4 个再继续
- 不要选一个就认为完成

**2. 检查下拉框（最高优先级！）**
- 有下拉框出现？→ **立即 click 选择！**
- 不要跳过下拉框去操作其他字段

**3. 识别页面布局**
- 左侧/顶部的职位信息区（只读）→ 不管它！
- 中间的表单区域 → 这才是要操作的

**4. 区分位置信息（最容易混淆！）**
- 左侧 "Location: XXX" → 这是**职位地点**，不管它！
- 表单 "Location*" 输入框 → 问**用户住哪里**
- 表单 "Which office" checkbox → 问**用户愿意在哪工作**

**5. 检查上一步结果**
- 上一步操作的字段是否正确？
- autocomplete 下拉框出现但没选中？→ 必须先 click 选择！
- 如果改错了 → 先修正！

**6. 按规划顺序执行**
- **下拉框出现** → 立即 click 选择
- autocomplete 显示 "Start typing..." → type 输入
- 空的普通必填字段 → fill 填写
- 页面有上传信号且需要简历/CV 时 → 使用 upload（value 填候选文件名或完整路径）
- checkbox 多选 → 按规划**逐个勾选**，全部完成再继续
- 都填好了且无错误提示 → Submit
- 感谢/确认信息 → done"""

        # 5. 调用 LLM（带模型降级机制）
        self._log(f"🤔 正在思考... (模型: {self.model})")
        
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                    },
                ],
            },
        ]
        #region agent log
        self._ndjson_log(
            hypothesis_id="H1",
            location="vision_agent:_observe_and_think:before_llm",
            message="pre LLM call",
            data={
                "model": self.model,
                "step": self.step_count,
                "screenshot_b64_len": len(screenshot_b64 or ""),
                "visible_text_len": len(visible_text),
                "upload_signals": upload_signals[:5],
                "upload_candidates_count": len(self.upload_candidates),
            },
        )
        #endregion

        raw = None
        while self.model_index < len(self.fallback_models):
            try:
                completion = self.client.chat.completions.create(
                    model=self.model,
                    temperature=self.llm_cfg.get("temperature", 0.2),
                    top_p=0.8,
                    max_tokens=self.llm_cfg.get("max_tokens", 1000),
                    messages=messages,
                )
                raw = completion.choices[0].message.content or ""
                #region agent log
                self._ndjson_log(
                    hypothesis_id="H2",
                    location="vision_agent:_observe_and_think:after_llm",
                    message="llm raw response",
                    data={
                        "model": self.model,
                        "step": self.step_count,
                        "raw_prefix": raw[:200],
                    },
                )
                #endregion
                break  # 成功则跳出
            except Exception as e:
                error_str = str(e)
                error_lower = error_str.lower()
                # 检测 429 Rate Limit 错误
                if "429" in error_str or "rate_limit" in error_lower:
                    self._log(f"⚠️ 模型 {self.model} 遇到速率限制", "warn")
                    # 尝试切换到下一个模型
                    self.model_index += 1
                    if self.model_index < len(self.fallback_models):
                        self.model = self.fallback_models[self.model_index]
                        self._log(f"🔄 切换到模型: {self.model}")
                        time.sleep(1)  # 短暂等待后重试
                    else:
                        self._log("❌ 所有模型都遇到速率限制，请稍后重试", "error")
                        return AgentState(status="error", summary="所有模型都遇到速率限制")
                # 模型能力不匹配（如不支持图片输入）时也尝试回退到下一个模型
                elif any(
                    kw in error_lower
                    for kw in [
                        "does not support",
                        "unsupported",
                        "multimodal",
                        "vision",
                        "image_url",
                        "invalid model",
                        "model_not_found",
                        "not found",
                    ]
                ):
                    self._log(f"⚠️ 模型 {self.model} 能力不匹配或不可用，尝试回退", "warn")
                    self.model_index += 1
                    if self.model_index < len(self.fallback_models):
                        self.model = self.fallback_models[self.model_index]
                        self._log(f"🔄 切换到模型: {self.model}")
                        time.sleep(1)
                    else:
                        self._log("❌ 所有候选模型都不支持当前请求", "error")
                        return AgentState(status="error", summary="所有候选模型都不支持当前请求")
                else:
                    self._log(f"❌ LLM 调用失败: {e}", "error")
                    return AgentState(status="error", summary=f"LLM 调用失败: {e}")
        
        if raw is None:
            return AgentState(status="error", summary="LLM 未返回结果")
        
        # 6. 解析返回
        data = self._safe_parse_json(raw)
        if not data:
            self._log(f"❌ LLM 返回格式错误: {raw[:300]}", "error")
            #region agent log
            self._ndjson_log(
                hypothesis_id="H3",
                location="vision_agent:_observe_and_think:parse_fail",
                message="parse fail",
                data={
                    "model": self.model,
                    "step": self.step_count,
                    "raw_prefix": raw[:200],
                },
            )
            #endregion
            return AgentState(status="error", summary="LLM 返回格式错误", raw_response=raw)
        
        status = data.get("status", "continue")
        summary = data.get("summary", "")
        
        next_action = None
        if status == "continue" and data.get("next_action"):
            act = data["next_action"]
            next_action = AgentAction(
                action=act.get("action", ""),
                ref=act.get("ref"),
                selector=act.get("selector"),
                value=act.get("value"),
                element_type=act.get("element_type"),
                reason=act.get("reason"),
            )
        
        return AgentState(
            status=status,
            summary=summary,
            next_action=next_action,
            raw_response=raw,
        )
    
    def _execute_action(self, action: AgentAction) -> bool:
        """
        执行单个操作，返回是否成功。
        根据 element_type 智能选择定位策略，像人类一样快速操作。
        """
        try:
            # 优先使用 ref 执行，降低误定位
            if action.ref:
                return self._execute_ref_action(action)

            if action.action == "click":
                if self._is_progression_action(action):
                    blocked_reason = self._get_progression_block_reason()
                    if blocked_reason:
                        self._log(f"⚠ 阻止盲目前进：{blocked_reason}", "warn")
                        return False
                return self._smart_click(action.selector, action.element_type)
            
            elif action.action == "fill":
                return self._smart_fill(action.selector, action.value)
            
            elif action.action == "type":
                return self._smart_type(action.selector, action.value)
            
            elif action.action == "select":
                return self._do_select(action.selector, action.value)

            elif action.action == "upload":
                return self._do_upload(action)
            
            elif action.action == "scroll":
                direction = action.value or action.selector or "down"
                return self._do_scroll(direction)
            
            elif action.action == "wait":
                seconds = int(action.value or 2)
                self.page.wait_for_timeout(seconds * 1000)
                return True
            
            elif action.action in ("done", "stuck"):
                return True
            
            else:
                self._log(f"未知操作类型: {action.action}", "warn")
                return False
                
        except Exception as e:
            self._log(f"执行异常: {e}", "error")
            return False

    def _execute_ref_action(self, action: AgentAction) -> bool:
        """基于快照 ref 执行动作（确定性定位）。"""
        item = self._last_snapshot_map.get(action.ref or "")
        if not item:
            self._log(f"ref 不存在: {action.ref}", "warn")
            return False

        locator = self._locator_from_snapshot_item(item)
        if locator is None:
            return False

        try:
            if action.action == "click":
                if self._is_progression_action(action, item=item):
                    blocked_reason = self._get_progression_block_reason()
                    if blocked_reason:
                        self._log(f"⚠ 阻止盲目前进：{blocked_reason}", "warn")
                        return False
                locator.click(timeout=1500)
                if self._verify_ref_action_effect(action, locator, item):
                    self._step_log("action_verify", {"action": action.action, "ref": action.ref, "ok": True})
                    return True
                ok = self._retry_ref_action(action, locator, item)
                self._step_log("action_verify", {"action": action.action, "ref": action.ref, "ok": ok})
                return ok
            if action.action == "fill":
                if action.value is None:
                    return False
                locator.fill(str(action.value), timeout=1500)
                if self._verify_ref_action_effect(action, locator, item):
                    self._step_log("action_verify", {"action": action.action, "ref": action.ref, "ok": True})
                    return True
                ok = self._retry_ref_action(action, locator, item)
                self._step_log("action_verify", {"action": action.action, "ref": action.ref, "ok": ok})
                return ok
            if action.action == "type":
                if action.value is None:
                    return False
                locator.click(timeout=800)
                locator.type(str(action.value), delay=40)
                if self._verify_ref_action_effect(action, locator, item):
                    self._step_log("action_verify", {"action": action.action, "ref": action.ref, "ok": True})
                    return True
                ok = self._retry_ref_action(action, locator, item)
                self._step_log("action_verify", {"action": action.action, "ref": action.ref, "ok": ok})
                return ok
            if action.action == "select":
                if action.value is None:
                    return False
                try:
                    locator.select_option(label=str(action.value), timeout=2000)
                except Exception:
                    locator.click(timeout=1500)
                if self._verify_ref_action_effect(action, locator, item):
                    self._step_log("action_verify", {"action": action.action, "ref": action.ref, "ok": True})
                    return True
                ok = self._retry_ref_action(action, locator, item)
                self._step_log("action_verify", {"action": action.action, "ref": action.ref, "ok": ok})
                return ok
            if action.action == "upload":
                return self._do_upload(action, locator=locator)
            if action.action == "scroll":
                direction = action.value or action.selector or "down"
                return self._do_scroll(direction)
            if action.action in ("wait", "done", "stuck"):
                if action.action == "wait":
                    seconds = int(action.value or 2)
                    self.page.wait_for_timeout(seconds * 1000)
                return True
        except Exception as e:
            self._log(f"ref 执行失败: {e}", "warn")
            return False

        return False

    def _locator_from_snapshot_item(self, item: SnapshotItem):
        """从快照项构建定位器。"""
        try:
            if item.role == "file_input":
                locator = self.page.locator("input[type='file']")
                return locator.nth(item.nth)
            locator = self.page.get_by_role(item.role, name=item.name)
            return locator.nth(item.nth)
        except Exception:
            return None

    def _detect_upload_signals(self, visible_text: str) -> list[str]:
        """
        检测页面是否存在“需要上传文件”的信号，避免盲目上传。
        """
        signals: list[str] = []

        try:
            input_count = self.page.locator("input[type='file']").count()
        except Exception:
            input_count = 0
        if input_count > 0:
            signals.append(f"input[type=file] x{input_count}")

        lower = (visible_text or "").lower()
        keywords = [
            "upload",
            "attach",
            "resume",
            "cv",
            "cover letter",
            "drop files",
            "choose file",
        ]
        for kw in keywords:
            if kw in lower:
                signals.append(f"text:{kw}")

        return signals

    def _is_progression_action(
        self,
        action: AgentAction,
        item: SnapshotItem | None = None,
    ) -> bool:
        if action.action != "click":
            return False
        name = ""
        if item is not None:
            name = item.name or ""
        elif action.selector:
            name = action.selector
        progression_keywords = [
            "next",
            "continue",
            "submit",
            "apply",
            "review",
            "proceed",
            "继续",
            "下一步",
            "提交",
            "申请",
        ]
        text = name.lower()
        return any(k in text for k in progression_keywords)

    def _get_progression_block_reason(self) -> str | None:
        """
        前进门控：存在明显错误或必填未填时，阻止 Next/Submit。
        """
        try:
            visible_text = self.page.inner_text("body")
        except Exception:
            visible_text = ""
        lower = (visible_text or "").lower()

        error_keywords = [
            "required",
            "missing",
            "invalid",
            "needs corrections",
            "please complete",
            "please fill",
            "error",
            "必填",
            "缺失",
            "错误",
        ]
        if any(k in lower for k in error_keywords):
            return "页面存在错误或必填缺失提示"

        missing_required = self._count_empty_required_fields()
        if missing_required > 0:
            return f"仍有 {missing_required} 个必填字段为空"

        return None

    def _count_empty_required_fields(self) -> int:
        """
        尝试统计当前快照里明显为空的 required 输入字段。
        """
        total = 0
        for item in self._last_snapshot_map.values():
            if not item.required:
                continue
            if item.role not in ("textbox", "combobox"):
                continue
            locator = self._locator_from_snapshot_item(item)
            if locator is None:
                continue
            value = self._get_input_value(locator).strip()
            if not value:
                total += 1
        return total

    def _verify_ref_action_effect(self, action: AgentAction, locator, item: SnapshotItem) -> bool:
        """对 ref 动作进行基础后验校验，失败则返回 False 触发重试。"""
        try:
            if action.action == "click":
                if item.role in ("checkbox", "radio"):
                    return locator.is_checked()
                return True
            if action.action in ("fill", "type", "select"):
                if action.value is None:
                    return True
                current = self._get_input_value(locator)
                target = str(action.value).strip()
                if target and target in (current or ""):
                    return True
                if action.action == "type" and item.role in ("combobox", "textbox"):
                    return self._is_dropdown_open(locator)
                return False
            if action.action == "upload":
                # upload 的 value 可能是文件名或完整路径；由 _verify_upload_success 统一确认
                if action.value:
                    ordered = resolve_upload_candidate(action.value, self.upload_candidates)
                    if ordered:
                        return self._verify_upload_success(ordered[0])
                return False
        except Exception:
            return False
        return True

    def _retry_ref_action(self, action: AgentAction, locator, item: SnapshotItem) -> bool:
        """当后验失败时，尝试一次更稳妥的补救动作。"""
        try:
            if action.action in ("fill", "type"):
                if action.value is None:
                    return False
                locator.fill(str(action.value), timeout=1500)
                return self._verify_ref_action_effect(action, locator, item)
            if action.action == "click" and item.role in ("checkbox", "radio"):
                try:
                    locator.check(timeout=1500)
                except Exception:
                    locator.click(timeout=1500)
                return self._verify_ref_action_effect(action, locator, item)
            if action.action == "click":
                try:
                    locator.scroll_into_view_if_needed(timeout=1500)
                    locator.click(timeout=1500)
                    return True
                except Exception:
                    return False
        except Exception:
            return False
        return False

    def _get_input_value(self, locator) -> str:
        """尽力获取输入框当前值。"""
        try:
            return locator.input_value(timeout=500)
        except Exception:
            try:
                return locator.evaluate("(el) => el.value || el.textContent || ''")
            except Exception:
                return ""

    def _is_dropdown_open(self, locator) -> bool:
        """检测 autocomplete 下拉是否打开（aria-expanded）。"""
        try:
            expanded = locator.get_attribute("aria-expanded")
            return str(expanded).lower() == "true"
        except Exception:
            return False

    def _step_log(self, event: str, payload: dict) -> None:
        """写入每步证据链日志。"""
        data = {
            "job_id": self.job_id,
            "event": event,
            "timestamp": int(time.time() * 1000),
            "payload": payload,
        }
        try:
            with open(self.trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception:
            pass
    
    def _smart_click(self, selector: str, element_type: str = None) -> bool:
        """
        智能点击：根据元素类型选择最佳策略。
        支持模糊匹配和滚动重试。
        """
        if not selector:
            return False
        
        timeout = 1000  # 每个策略 1 秒
        check_timeout = 200  # 可见性检查 200ms
        
        # 清理 selector：去除重复词（如 "Dallas Dallas" → "Dallas"）
        words = selector.split()
        seen = set()
        unique_words = []
        for w in words:
            if w.lower() not in seen:
                seen.add(w.lower())
                unique_words.append(w)
        clean_selector = " ".join(unique_words)
        
        # 从复杂 selector 中提取简短关键词（如 "Yes"、"No"）
        short_selector = clean_selector
        if " " in clean_selector and len(clean_selector) > 20:
            # 如果 selector 很长，尝试取最后一个词（通常是 Yes/No）
            if unique_words[-1] in ["Yes", "No", "yes", "no"]:
                short_selector = unique_words[-1]
        
        # 提取第一个关键词用于模糊匹配（如 "Dallas" 匹配 "Dallas, TX"）
        first_word = unique_words[0] if unique_words else clean_selector
        
        # 根据元素类型选择策略
        if element_type == "button":
            strategies = [
                lambda: self.page.get_by_role("button", name=clean_selector).first,
                lambda: self.page.get_by_text(clean_selector, exact=False).first,
            ]
        elif element_type == "link":
            strategies = [
                lambda: self.page.get_by_role("link", name=clean_selector).first,
                lambda: self.page.get_by_text(clean_selector, exact=False).first,
            ]
        elif element_type in ("checkbox", "radio"):
            # checkbox/radio 支持多种匹配方式
            strategies = [
                # 1. 直接点击短文本（Yes/No）
                lambda: self.page.get_by_role("button", name=short_selector).first,
                lambda: self.page.get_by_text(short_selector, exact=True).first,
                # 2. 尝试 radio/checkbox 角色
                lambda: self.page.get_by_role(element_type, name=short_selector).first,
                lambda: self.page.get_by_label(short_selector).first,
                # 3. 用清理后的 selector
                lambda: self.page.get_by_text(clean_selector, exact=True).first,
                lambda: self.page.get_by_text(clean_selector, exact=False).first,
                # 4. 模糊匹配：用第一个词（如 Dallas 匹配 "Dallas, TX"）
                lambda: self.page.get_by_text(first_word, exact=False).first,
                lambda: self.page.get_by_label(first_word, exact=False).first,
                # 5. CSS 选择器模糊匹配
                lambda: self.page.locator(f"label:has-text('{first_word}')").first,
                lambda: self.page.locator(f"[data-testid*='{first_word}' i]").first,
            ]
        elif element_type == "option":
            strategies = [
                lambda: self.page.get_by_role("option", name=clean_selector).first,
                lambda: self.page.get_by_text(clean_selector, exact=True).first,
                lambda: self.page.get_by_text(clean_selector, exact=False).first,
                lambda: self.page.locator(f"li:has-text('{clean_selector}')").first,
                # 模糊匹配
                lambda: self.page.get_by_role("option", name=first_word).first,
                lambda: self.page.get_by_text(first_word, exact=False).first,
                lambda: self.page.locator(f"li:has-text('{first_word}')").first,
            ]
        else:
            strategies = [
                lambda: self.page.get_by_text(clean_selector, exact=True).first,
                lambda: self.page.get_by_role("button", name=clean_selector).first,
                lambda: self.page.get_by_text(clean_selector, exact=False).first,
                # 模糊匹配
                lambda: self.page.get_by_text(first_word, exact=False).first,
            ]
        
        # 尝试点击（带滚动重试）
        max_scroll_attempts = 2
        for scroll_attempt in range(max_scroll_attempts + 1):
            for strategy in strategies:
                try:
                    locator = strategy()
                    if locator and locator.is_visible(timeout=check_timeout):
                        locator.click(timeout=timeout)
                        return True
                except Exception:
                    continue
            
            # 如果所有策略都失败，尝试滚动页面后重试
            if scroll_attempt < max_scroll_attempts:
                try:
                    self.page.evaluate("window.scrollBy(0, 300)")
                    self.page.wait_for_timeout(300)
                    self._log(f"   🔄 滚动页面，重试定位 ({scroll_attempt + 1}/{max_scroll_attempts})")
                except Exception:
                    break
        
        return False
    
    def _smart_fill(self, selector: str, value: str) -> bool:
        """智能填写：通过 label 精确定位输入框并填写"""
        if not selector or value is None:
            return False
        
        timeout = 1500
        value_str = str(value)
        clean_selector = selector.replace("*", "").strip()
        
        # 只用基于 label 的精确定位，避免误定位到其他输入框
        strategies = [
            lambda: self.page.get_by_label(selector, exact=False).first,
            lambda: self.page.get_by_label(clean_selector, exact=False).first,
            lambda: self.page.get_by_role("textbox", name=selector).first,
            lambda: self.page.get_by_role("textbox", name=clean_selector).first,
            # 通过 label 文本找相邻输入框
            lambda: self.page.locator(f"label:has-text('{clean_selector}')").locator("..").locator("input").first,
        ]
        # 注意：不要用 get_by_placeholder 这种宽泛匹配，容易定位到错误字段
        
        for strategy in strategies:
            try:
                locator = strategy()
                if locator.is_visible(timeout=200):
                    locator.fill(value_str, timeout=timeout)
                    return True
            except Exception:
                continue
        
        return False
    
    def _smart_type(self, selector: str, value: str) -> bool:
        """
        智能输入：逐字输入触发 autocomplete 下拉框。
        
        处理各种 autocomplete 输入框：
        - 带 * 的 label（如 "Location*"）
        - placeholder 提示（如 "Start typing..."）
        - combobox 类型的输入框
        """
        if not selector or value is None:
            return False
        
        # 清理 selector（去掉可能的 * 和多余空格）
        clean_selector = selector.replace("*", "").strip()
        
        # 快速找到输入框 - 优先使用 label 匹配，避免误定位
        input_elem = None
        strategies = [
            # 1. 精确 label 匹配（最可靠）
            lambda: self.page.get_by_label(selector, exact=False).first,
            # 2. 清理后的 label 匹配
            lambda: self.page.get_by_label(clean_selector, exact=False).first,
            # 3. combobox 角色（autocomplete 通常是 combobox）
            lambda: self.page.get_by_role("combobox", name=selector).first,
            lambda: self.page.get_by_role("combobox", name=clean_selector).first,
            # 4. textbox 角色
            lambda: self.page.get_by_role("textbox", name=selector).first,
            lambda: self.page.get_by_role("textbox", name=clean_selector).first,
            # 5. 通过包含 selector 文本的 label 元素找相邻输入框
            lambda: self.page.locator(f"label:has-text('{clean_selector}')").locator("..").locator("input, [role='combobox']").first,
            # 6. 直接通过 aria-label
            lambda: self.page.locator(f"[aria-label*='{clean_selector}' i]").first,
        ]
        # 注意：不要用 get_by_placeholder("type") 这种宽泛匹配，容易定位到错误字段
        
        for strategy in strategies:
            try:
                elem = strategy()
                if elem.is_visible(timeout=300):
                    input_elem = elem
                    self._log(f"   📍 定位成功: {selector}")
                    break
            except Exception:
                continue
        
        if not input_elem:
            self._log(f"   ⚠️ 无法定位输入框: {selector}", "warn")
            return False
        
        try:
            # 1. 点击激活输入框
            input_elem.click(timeout=800)
            self.page.wait_for_timeout(100)
            
            # 2. 清空现有内容（全选后删除，更可靠）
            input_elem.press("Control+a")
            self.page.wait_for_timeout(30)
            input_elem.press("Backspace")
            self.page.wait_for_timeout(50)
            
            # 3. 逐字输入，触发 autocomplete
            input_elem.type(str(value), delay=40)  # 逐字输入触发下拉
            
            # 4. 短暂等待让下拉框出现（主循环会截图让 AI 看到变化）
            self.page.wait_for_timeout(600)
            return True
        except Exception as e:
            self._log(f"   ⚠️ 输入失败: {e}", "warn")
            return False
    
    
    def _do_select(self, selector: str, value: str) -> bool:
        """
        选择下拉框选项（仅处理原生 <select>）。
        对于非原生下拉框，AI 应该使用 type + click 组合。
        """
        if not selector or not value:
            return False
        
        # 只尝试原生 select，其他情况让 AI 用 type + click
        try:
            select = self.page.get_by_label(selector).first
            if select.is_visible(timeout=500):
                select.select_option(label=value, timeout=2000)
                return True
        except Exception:
            pass
        
        # 尝试直接点击已显示的选项（下拉框可能已经打开）
        try:
            option = self.page.get_by_role("option", name=value).first
            if option.is_visible(timeout=300):
                option.click(timeout=1500)
                return True
        except Exception:
            pass
        
        return False

    def _do_upload(self, action: AgentAction, locator=None) -> bool:
        """
        执行可控文件上传：
        - 必须先检测到上传信号
        - 仅允许白名单目录内文件
        - 上传失败可重试并尝试候选文件回退
        """
        if not self._last_upload_signals:
            self._log("⚠ 页面无上传信号，跳过 upload 动作", "warn")
            return False

        ordered_candidates = resolve_upload_candidate(
            action.value,
            self.upload_candidates,
        )
        # 任务预选简历优先（阶段A），失败再回退候选列表
        if self.preferred_resume_path:
            preferred = self.preferred_resume_path
            if is_upload_path_allowed(preferred):
                ordered_candidates = [preferred] + [c for c in ordered_candidates if c != preferred]

        if not ordered_candidates:
            self._log("⚠ 无可用上传候选文件（白名单目录为空）", "warn")
            return False

        max_attempts = min(3, len(ordered_candidates))
        for attempt_idx in range(max_attempts):
            candidate = ordered_candidates[attempt_idx]
            if not is_upload_path_allowed(candidate):
                self._log(f"⚠ 拒绝非白名单路径: {candidate}", "warn")
                continue

            target_locator = locator
            if target_locator is None:
                target_locator = self._locate_file_input(action.selector)
            if target_locator is None:
                self._log("⚠ 未定位到 file input，无法上传", "warn")
                return False

            try:
                target_locator.set_input_files(candidate, timeout=5000)
            except Exception as exc:
                self._log(
                    f"⚠ 上传失败，attempt={attempt_idx + 1}, file={Path(candidate).name}, err={exc}",
                    "warn",
                )
                continue

            if self._verify_upload_success(candidate):
                self._log(
                    f"✓ 上传成功，attempt={attempt_idx + 1}, file={Path(candidate).name}"
                )
                return True

            self._log(
                f"⚠ 上传后未确认成功，attempt={attempt_idx + 1}, file={Path(candidate).name}",
                "warn",
            )

        return False

    def _locate_file_input(self, selector: str | None):
        """
        尝试定位文件上传 input。
        """
        try:
            file_inputs = self.page.locator("input[type='file']")
            if file_inputs.count() > 0:
                return file_inputs.first
        except Exception:
            pass

        if selector:
            # 有些页面需要先点“Upload/Attach”按钮再出现 file input
            self._smart_click(selector, element_type="button")
            self.page.wait_for_timeout(300)
            try:
                file_inputs = self.page.locator("input[type='file']")
                if file_inputs.count() > 0:
                    return file_inputs.first
            except Exception:
                pass
        return None

    def _verify_upload_success(self, file_path: str) -> bool:
        """
        上传成功确认（多信号）：
        - input.files 非空且文件名匹配
        - 或页面文本出现文件名
        """
        filename = Path(file_path).name

        try:
            count = self.page.locator("input[type='file']").count()
        except Exception:
            count = 0

        for i in range(count):
            try:
                locator = self.page.locator("input[type='file']").nth(i)
                ok = locator.evaluate(
                    "(el, expected) => (el.files && el.files.length > 0 && el.files[0].name === expected)",
                    filename,
                )
                if ok:
                    return True
            except Exception:
                continue

        try:
            body_text = self.page.inner_text("body")
            if filename in body_text:
                return True
        except Exception:
            pass

        return False
    
    def _do_scroll(self, direction: str) -> bool:
        """滚动页面"""
        try:
            if "down" in direction.lower():
                self.page.evaluate("window.scrollBy(0, 500)")
            else:
                self.page.evaluate("window.scrollBy(0, -500)")
            return True
        except Exception:
            return False
    
    def _verify_completion(self) -> tuple[bool, str]:
        """
        二次验证：检查页面是否真的完成了申请。
        
        关键逻辑：
        1. 如果 Submit 按钮仍可见且没有成功消息 → 表单未提交
        2. 排除浏览器扩展消息（如 "Autofill complete!"）的干扰
        3. 必须有明确的成功消息才算完成
        
        返回:
            tuple[bool, str]: (是否真的完成, 验证信息)
        """
        try:
            # 获取页面文本
            body_text = self.page.inner_text("body").lower()
            
            # 1. 检查是否有真正的成功标志（必须是网站返回的，不是扩展）
            success_indicators = [
                "thank you for applying",
                "thanks for your application",
                "application submitted",
                "application received",
                "successfully submitted",
                "we have received your application",
                "your application has been submitted",
                "application complete",
                "thanks for submitting",
                "we'll be in touch",
                "we will review your application",
            ]
            
            has_success = any(indicator in body_text for indicator in success_indicators)
            
            # 2. 排除浏览器扩展的误报消息
            extension_false_positives = [
                "autofill complete",
                "simplify",
                "extension",
                "chrome extension",
            ]
            
            # 如果页面只有扩展相关的"成功"消息，不算真正成功
            if not has_success:
                for fp in extension_false_positives:
                    if fp in body_text and "complete" in body_text:
                        self._log(f"   ⚠ 检测到扩展消息 '{fp}'，不是真正的申请成功")
            
            # 3. 检查是否有错误标志
            error_indicators = [
                "this field is required",
                "please fill",
                "is required",
                "missing required",
                "please complete",
                "invalid",
            ]
            
            has_error = False
            for indicator in error_indicators:
                if indicator in body_text:
                    has_error = True
                    break
            
            # 4. 检查是否还有 Submit 按钮可见（关键检查！）
            has_submit_button = False
            submit_button_checks = [
                ("button", "Submit"),
                ("button", "Submit Application"),
                ("button", "Apply"),
                ("button", "Submit your application"),
            ]
            
            for role, name in submit_button_checks:
                try:
                    submit_btn = self.page.get_by_role(role, name=name).first
                    if submit_btn.is_visible(timeout=300):
                        has_submit_button = True
                        self._log(f"   🔍 检测到 Submit 按钮仍可见: '{name}'")
                        break
                except Exception:
                    continue
            
            # 也检查文本匹配
            if not has_submit_button:
                try:
                    submit_text = self.page.get_by_text("Submit Application", exact=False).first
                    if submit_text.is_visible(timeout=300):
                        has_submit_button = True
                        self._log("   🔍 检测到 Submit Application 文本仍可见")
                except Exception:
                    pass
            
            # 5. 综合判断
            # 关键规则：如果 Submit 按钮仍可见且没有成功消息，表单肯定未提交
            if has_submit_button and not has_success:
                return False, "Submit 按钮仍可见，表单尚未提交"
            
            if has_error:
                return False, "页面仍有错误提示，表单未完成"
            
            if has_success and not has_error:
                return True, "页面显示申请成功信息，无错误提示"
            
            # 如果没有成功标志也没有 Submit 按钮，可能是跳转到了其他页面
            if not has_success and not has_submit_button:
                # 保守判断，可能需要继续观察
                return False, "未检测到明确的成功信息，可能需要继续"
            
            return False, "状态不确定，继续执行"
            
        except Exception as e:
            self._log(f"⚠ 二次验证出错: {e}", "warn")
            # 验证出错时，保守返回 False
            return False, f"验证过程出错: {e}"
    
    def _compress_screenshot(self, png_bytes: bytes) -> bytes:
        """
        压缩截图：PNG → JPEG，限制宽度，降低体积但保证识别质量。
        
        压缩策略：
        - 转换为 JPEG 格式（比 PNG 体积小很多）
        - 限制最大宽度为 1280px（足够 LLM 识别文字和 UI 元素）
        - JPEG 质量 75（清晰度和体积的良好平衡）
        """
        try:
            # 打开 PNG 图片
            img = Image.open(io.BytesIO(png_bytes))
            
            # 如果宽度超过限制，等比例缩小
            if img.width > SCREENSHOT_MAX_WIDTH:
                ratio = SCREENSHOT_MAX_WIDTH / img.width
                new_height = int(img.height * ratio)
                img = img.resize((SCREENSHOT_MAX_WIDTH, new_height), Image.Resampling.LANCZOS)
            
            # 转换为 RGB（JPEG 不支持 RGBA）
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            
            # 保存为 JPEG
            output = io.BytesIO()
            img.save(output, format="JPEG", quality=SCREENSHOT_JPEG_QUALITY, optimize=True)
            return output.getvalue()
        except Exception as e:
            # 压缩失败时返回原始 PNG
            self._log(f"⚠️ 截图压缩失败，使用原图: {e}", "warn")
            return png_bytes
    
    def _safe_parse_json(self, raw: str) -> dict | None:
        """安全解析 JSON"""
        # 直接解析
        try:
            return json.loads(raw)
        except Exception:
            pass
        
        # 从 markdown 代码块提取
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
        
        # 提取 { ... }
        if "{" in raw and "}" in raw:
            try:
                start = raw.index("{")
                end = raw.rfind("}") + 1
                return json.loads(raw[start:end])
            except Exception:
                pass
        
        return None
    
    def _log(self, message: str, level: str = "info") -> None:
        """写入日志"""
        with SessionLocal() as session:
            session.add(JobLog(job_id=self.job_id, level=level, message=message))
            session.commit()
        print(f"[job={self.job_id}] [{level.upper()}] {message}")


# 便捷函数
def run_browser_agent(page: Page, job, max_steps: int = 50) -> bool:
    """运行浏览器 Agent"""
    agent = BrowserAgent(page, job, max_steps)
    return agent.run()
