from __future__ import annotations

import json
import os
import platform
import re
import socket
import sys
import uuid
from pathlib import Path
from typing import Any

from . import APP_VERSION


def default_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "HamshmarehExtractor"
    return Path.home() / ".hamshmareh-extractor"


DATA_DIR = default_data_dir()
SETTINGS_FILE = DATA_DIR / "settings.json"
TOKEN_FILE = DATA_DIR / "device-token.bin"
STATE_DB = DATA_DIR / "agent.sqlite3"
LOG_DIR = DATA_DIR / "logs"


def _has_extraction_state(path: Path) -> bool:
    try:
        return path.is_dir() and any(item.is_file() for item in path.glob("*/state.sqlite3"))
    except OSError:
        return False


def resolve_output_dir(
    *,
    data_dir: Path,
    project_root: Path,
    cwd: Path,
    executable_path: Path,
    frozen: bool,
) -> Path:
    """Select a local output root while preserving the legacy resumable state."""
    configured = os.environ.get("HAMSHMAREH_OUTPUT_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()

    candidates: list[Path] = []
    if not frozen:
        candidates.append(project_root / "output")
    candidates.append(cwd / "output")
    executable_dir = executable_path.parent
    candidates.append(executable_dir / "output")
    if frozen:
        candidates.append(executable_dir.parent / "output")

    for candidate in candidates:
        if _has_extraction_state(candidate):
            return candidate

    return data_dir / "extraction" if frozen else project_root / "output"


OUTPUT_DIR = resolve_output_dir(
    data_dir=DATA_DIR,
    project_root=Path(__file__).resolve().parents[1],
    cwd=Path.cwd(),
    executable_path=Path(sys.executable).resolve(),
    frozen=bool(getattr(sys, "frozen", False)),
)


def _safe_device_name() -> str:
    value = os.environ.get("COMPUTERNAME") or socket.gethostname() or platform.node()
    value = re.sub(r"[^\w .-]", "", value, flags=re.UNICODE).strip()
    return value[:160] or "Hamshmareh Desktop"


def ensure_device_id(settings: dict[str, Any]) -> str:
    value = str(settings.get("device_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value):
        value = uuid.uuid4().hex
        settings["device_id"] = value
    return value


def default_settings() -> dict[str, Any]:
    return {
        "api_base_url": os.environ.get("HAMSHMAREH_API_BASE_URL", "http://localhost:5050/api").rstrip("/"),
        "device_id": uuid.uuid4().hex,
        "device_name": _safe_device_name(),
        "bot_delay_seconds": 1.0,
        "bot_max_retries": 6,
        "bot_rate_limit_cooldown_seconds": 600.0,
        "request_timeout_seconds": 45.0,
        "batch_size": 50,
        "batch_interval_seconds": 10.0,
        "app_version": APP_VERSION,
    }


def load_settings() -> dict[str, Any]:
    settings = default_settings()
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            settings.update(raw)
    except (OSError, ValueError, TypeError):
        pass
    ensure_device_id(settings)
    settings["api_base_url"] = str(settings.get("api_base_url") or "").strip().rstrip("/")
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    normalized = {**default_settings(), **settings, "app_version": APP_VERSION}
    ensure_device_id(normalized)
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SETTINGS_FILE)
