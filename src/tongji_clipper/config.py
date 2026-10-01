"""Configuration, local paths, and runtime security tokens for Tongji Clipper."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlunparse

SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_\-\.]{1,128}$")


def redact_urls(text: str) -> str:
    return re.sub(r'https?://[^\s\"\'<>]+', lambda m: sanitize_url(m.group(0)), str(text))


def assert_safe_id(val: str, field_name: str) -> str:
    """Validates that an identifier contains only path-safe characters and is not traversal."""
    if not val or not isinstance(val, str):
        raise ValueError(f"{field_name} must be a non-empty string")
    clean = val.strip()
    if clean in (".", "..") or not SAFE_ID_RE.match(clean):
        raise ValueError(
            f"Invalid {field_name}: '{val}' contains unsafe path characters or is too long (allowed: letters, digits, _, -, .)"
        )
    return clean


def sanitize_url(url: Optional[str]) -> str:
    """Strips query parameters (signatures/tokens) and userinfo from URL for safe logging."""
    if not url:
        return ""
    try:
        p = urlparse(url)
        # Remove userinfo (password/user) and query params
        netloc = p.netloc
        if "@" in netloc:
            netloc = netloc.split("@")[-1]
        cleaned = p._replace(netloc=netloc, query="", fragment="")
        return urlunparse(cleaned)
    except Exception:
        return "<sanitized-url>"


def get_app_dir() -> Path:
    """
    Returns app runtime directory.
    Priority:
    1. TONGJI_CLIPPER_HOME environment variable
    2. Project local .runtime directory (workspace independent)
    3. User home ~/.tongji_clipper fallback
    """
    env_home = os.environ.get("TONGJI_CLIPPER_HOME")
    if env_home:
        p = Path(env_home).resolve()
    else:
        try:
            p = (Path(__file__).resolve().parents[2] / ".runtime").resolve()
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            p = Path.home() / ".tongji_clipper"

    p.mkdir(parents=True, exist_ok=True)
    return p


def get_db_path() -> Path:
    """Returns SQLite database path."""
    return get_app_dir() / "queue.db"


def get_worker_info_path() -> Path:
    """Returns runtime worker info file path containing port, pid, and auth token."""
    return get_app_dir() / "worker.json"


def get_default_output_dir() -> Path:
    """Default directory for transcripts."""
    out = Path(__file__).resolve().parents[2] / "output"
    out.mkdir(parents=True, exist_ok=True)
    return out


def get_models_cache_dir() -> Path:
    """Default local cache directory for faster-whisper models."""
    env_models = os.environ.get("TONGJI_CLIPPER_MODELS_DIR")
    if env_models:
        p = Path(env_models).resolve()
    else:
        p = get_app_dir() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_worker_info() -> Optional[Dict[str, Any]]:
    path = get_worker_info_path()
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_worker_info(port: int, pid: int, token: str) -> None:
    """Atomically writes worker info file."""
    path = get_worker_info_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    data = {
        "port": port,
        "pid": pid,
        "token": token,
    }
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def remove_worker_info() -> None:
    path = get_worker_info_path()
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


def generate_auth_token() -> str:
    """Generates a cryptographically strong random token for localhost worker auth."""
    return secrets.token_urlsafe(32)
