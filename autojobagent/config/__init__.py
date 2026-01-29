"""
Configuration module for loading user profile and settings.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml


# Config directory path
CONFIG_DIR = Path(__file__).parent
USER_PROFILE_PATH = CONFIG_DIR / "user_profile.yaml"
AGENT_GUIDELINES_PATH = CONFIG_DIR / "agent_guidelines.md"


_user_profile_cache: Optional[dict] = None
_agent_guidelines_cache: Optional[str] = None


def load_user_profile(force_reload: bool = False) -> dict:
    """
    Load user profile from YAML file.
    Caches the result for performance.
    
    Returns:
        dict: User profile data
    """
    global _user_profile_cache
    
    if _user_profile_cache is not None and not force_reload:
        return _user_profile_cache
    
    if not USER_PROFILE_PATH.exists():
        print(f"⚠️ User profile not found: {USER_PROFILE_PATH}")
        return {}
    
    try:
        with open(USER_PROFILE_PATH, "r", encoding="utf-8") as f:
            _user_profile_cache = yaml.safe_load(f) or {}
        return _user_profile_cache
    except Exception as e:
        print(f"❌ Failed to load user profile: {e}")
        return {}


def get_user_info_for_prompt() -> str:
    """
    Generate a formatted string of user information for LLM prompt injection.
    
    Returns:
        str: Formatted user information for the AI to use when filling forms
    """
    profile = load_user_profile()
    if not profile:
        return "（用户信息未配置）"
    
    personal = profile.get("personal", {})
    location = profile.get("location", {})
    work_auth = profile.get("work_authorization", {})
    work_pref = profile.get("work_preferences", {})
    demographics = profile.get("demographics", {})
    education = profile.get("education", {})
    experience = profile.get("experience", {})
    common = profile.get("common_answers", {})
    
    # Get highest degree info
    degrees = education.get("degrees", [])
    highest_degree = degrees[0] if degrees else {}
    
    # 当前位置
    current_city = location.get('current_city', '')
    current_full = location.get('full_location', '')
    
    # 偏好工作位置（取前3个）
    preferred_locs = work_pref.get('preferred_locations', [])[:3]
    preferred_str = ", ".join(preferred_locs) if preferred_locs else "同当前位置"
    
    info = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔴 用户真实信息 - 必须使用，不要编造！
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 基本信息
- 👤 Name: {personal.get('full_name', '')} (First: {personal.get('first_name', '')}, Last: {personal.get('last_name', '')})
- 📧 Email: {personal.get('email', '')} 或 {personal.get('email_alternate', '')}
- 📱 Phone: {personal.get('phone', '')}
- 🔗 LinkedIn: {personal.get('linkedin', '')}

## 📍 位置相关信息（⚠️ 最容易混淆，仔细看！）

**页面上可能出现三种"位置"信息，必须区分清楚：**

| 你看到的内容 | 在哪里 | 是什么 | 你应该怎么做 |
|-------------|--------|--------|-------------|
| `Location: Boston, NYC` | 左侧职位信息区 | 职位的办公地点 | **忽略它！只读信息！** |
| `Location*` 输入框 | 表单区域 | 问"你住哪里" | 填 **{current_city}** |
| `Which office...` checkbox | 表单区域 | 问"你愿意在哪工作" | 选配置中的城市 |

**🚨 常见错误（绝对禁止！）：**
- ❌ 看到左侧显示 "Location: Boston, NYC"，就以为 Location 字段要填 Boston
- ❌ 把 Location 输入框 和 Which office checkbox 混为一谈
- ❌ 认为用户居住地应该和职位地点一致

**✅ 正确理解：**
- 用户住在 {current_city}，这和职位在 Boston/NYC 完全没关系
- Location 表单字段问的是"你住哪里" → 填 {current_city}
- Which office 问的是"你愿意去哪工作" → 从下面列表选

**Which office checkbox 的正确处理方式（取交集 + 模糊匹配）：**

用户偏好城市列表：
• San Francisco ✓ • Los Angeles ✓ • Seattle ✓ • Dallas ✓
• Austin ✓ • New York ✓ • Boston ✓

**处理步骤：**
1. 看页面有哪些选项
2. 取交集 = 用户偏好 ∩ 页面选项
3. 模糊匹配：Boston = Boston (Cambridge)，NYC = New York City
4. 交集有几个就选几个，全部勾选！

**示例：**
```
页面选项: [Boston (Cambridge), NYC (Chelsea), LA (Venice), SF, Remote only]
用户偏好: [Boston, New York, SF, LA, Dallas, Seattle, Austin]
交集: Boston (Cambridge)、NYC (Chelsea)、LA (Venice)、SF
执行: 勾选这 4 个，排除 Remote only
```

→ ❌ 禁止选择 "Remote only"（用户偏好没有）
→ 规划了 N 个就必须选 N 个，不要选一个就停！
→ 已选的有效城市不要取消！

## 工作授权
- Authorized to work in US: {"Yes" if work_auth.get('authorized_to_work_in_us') else "No"}
- Require visa sponsorship: {"Yes" if work_auth.get('require_visa_sponsorship') else "No"}
- Current visa: {work_auth.get('current_visa_status', '')}

## 人口统计（Voluntary Self-Identification）
- Gender: {demographics.get('gender', '')}
- Ethnicity/Race: {demographics.get('ethnicity', '')}
- Veteran: {demographics.get('veteran_status', '')}
- Disability: {demographics.get('disability_status', '')}

## 教育背景
- Degree: {education.get('highest_degree', '')} in {highest_degree.get('field', '')}
- University: {highest_degree.get('university', '')}
- Graduation: {highest_degree.get('end_date', '')}

## 工作经验
- Years: {experience.get('years_of_experience', '')}
- Current: {experience.get('current_title', '')} @ {experience.get('current_company', '')}

## 其他
- Salary: {work_pref.get('salary_expectation', '')}
- Start Date: {work_pref.get('earliest_start_date', '')}
- Zip Code: {location.get('zip_code', '')}

## 📋 常见问题快速回答（规划时直接使用！）

| 问题 | 答案 |
|------|------|
| "Do you have a relative at this company?" | **{"No" if not common.get('has_relative_at_company') else "Yes"}** |
| "Have you previously worked at this company?" | **{"No" if not common.get('previously_worked_at_company') else "Yes"}** |
| "Are you at least 18 years old?" | **{"Yes" if common.get('is_over_18') else "No"}** |
| "Do you have a valid driver's license?" | **{"Yes" if common.get('has_drivers_license') else "No"}** |
| "Willing to undergo background check?" | **{"Yes" if common.get('willing_background_check') else "No"}** |
| "Willing to take drug test?" | **{"Yes" if common.get('willing_drug_test') else "No"}** |
| "How did you hear about this position?" | **{common.get('referral_source', 'LinkedIn')}** |
| 其他未知问题（家中有政府人员？等） | **默认回答 No 或 N/A** |

## 🎯 规划时的具体值（直接使用！）

- **Location 输入框** → 填 **"{current_full}"**
- **Which office checkbox** → 取**交集**后**全部勾选**（模糊匹配城市名）
- **Work authorization** → **Yes**
- **Visa sponsorship** → **Yes**
- **Gender** → **{demographics.get('gender', 'Male')}**
- **Ethnicity** → **{demographics.get('ethnicity', 'Asian')}**
- **Veteran** → **{demographics.get('veteran_status', 'No')}**
- **Disability** → **{demographics.get('disability_status', 'No')}**

## 🔑 模糊匹配原则（名称不完全相同时）

页面选项可能包含额外信息（州、括号备注等），只要推理判断是同一事物就匹配：

| 用户偏好 | 页面选项 | 匹配？ |
|---------|---------|-------|
| Boston | Boston (Cambridge) | ✓ |
| New York | NYC (Chelsea) | ✓ |
| New York | New York City | ✓ |
| SF | San Francisco | ✓ |
| LA | Los Angeles (Venice) | ✓ |

**规则：匹配后使用页面显示的完整名称进行点击**

## 📝 开放式问题处理

没有选项的问题（如"技能"、"偏好城市"）：
- 从用户资料提取相关信息
- 默认填 3 个有效值，用逗号分隔
- 示例：Skills → "Python, Machine Learning, Deep Learning"
"""
    return info.strip()


def get_allowed_upload_directories() -> list[str]:
    """
    Get the list of allowed directories for file uploads.
    
    Returns:
        list[str]: List of allowed directory paths
    """
    profile = load_user_profile()
    files_config = profile.get("files", {})
    return files_config.get("allowed_directories", [])


def get_default_resume_path() -> str:
    """
    Get the default resume file path.
    
    Returns:
        str: Path to default resume file
    """
    profile = load_user_profile()
    files_config = profile.get("files", {})
    return files_config.get("default_resume", "")


def load_agent_guidelines(force_reload: bool = False) -> str:
    """
    Load Agent operation guidelines from Markdown file.
    Caches the result for performance.
    
    Returns:
        str: Agent guidelines content
    """
    global _agent_guidelines_cache
    
    if _agent_guidelines_cache is not None and not force_reload:
        return _agent_guidelines_cache
    
    if not AGENT_GUIDELINES_PATH.exists():
        print(f"⚠️ Agent guidelines not found: {AGENT_GUIDELINES_PATH}")
        return ""
    
    try:
        _agent_guidelines_cache = AGENT_GUIDELINES_PATH.read_text(encoding="utf-8")
        return _agent_guidelines_cache
    except Exception as e:
        print(f"❌ Failed to load agent guidelines: {e}")
        return ""
