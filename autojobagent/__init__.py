"""
Job Autopilot - Auto Application Agent

MVP 骨架包初始化文件。
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import find_dotenv, load_dotenv
except Exception:  # pragma: no cover - optional dependency in local env
    find_dotenv = None
    load_dotenv = None

# Auto-load project .env once on package import when python-dotenv is available.
# This lets local runs work without manually exporting OPENAI_API_KEY each time.
if find_dotenv and load_dotenv:
    load_dotenv(find_dotenv(usecwd=True), override=False)
else:
    # Fallback minimal .env loader when python-dotenv is unavailable.
    env_path = Path.cwd() / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
