from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from opencc import OpenCC


APP_NAME = "Tongji Course Clipper"
VERSION = "0.2.0"
ACTIVE_STATES = {"queued", "downloading", "transcribing", "writing"}
ALLOWED_MODELS = {"tiny", "base", "small", "medium"}
T2S = OpenCC("t2s")


@dataclass(frozen=True)
class Settings:
    vault_dir: Path
    notes_subdir: str
    model: str
    language: str
    host: str
    port: int
    keep_temporary_media: bool
    model_cache_dir: Path
    direct_output_dir: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "Settings":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_raw(raw, path)

    @classmethod
    def from_raw(cls, raw: dict[str, Any], path: Path) -> "Settings":
        raw_output_dir = str(raw.get("output_dir", "")).strip()
        model = str(raw.get("model", "small"))
        if model not in ALLOWED_MODELS:
            raise ValueError(f"不支持的 Whisper 模型：{model}")
        cache = Path(raw.get("model_cache_dir", ".cache/models"))
        if not cache.is_absolute():
            cache = (path.parent / cache).resolve()
        if raw_output_dir:
            output_dir = validate_output_dir(raw_output_dir)
            return cls(
                vault_dir=output_dir,
                notes_subdir="",
                model=model,
                language=raw.get("language", "zh"),
                host=raw.get("host", "127.0.0.1"),
                port=int(raw.get("port", 8765)),
                keep_temporary_media=bool(raw.get("keep_temporary_media", False)),
                model_cache_dir=cache,
                direct_output_dir=output_dir,
            )
        raw_vault_dir = str(raw.get("vault_dir", "")).strip()
        if not raw_vault_dir or re.search(r"Path[\\/]To[\\/]Your", raw_vault_dir, re.IGNORECASE):
            raise ValueError("Obsidian Vault 尚未配置")
        vault_dir = Path(os.path.expandvars(raw_vault_dir)).expanduser()
        if not vault_dir.is_absolute():
            vault_dir = (path.parent / vault_dir).resolve()
        notes_subdir = validate_notes_subdir(str(raw.get("notes_subdir", "课程笔记")))
        return cls(
            vault_dir=vault_dir,
            notes_subdir=notes_subdir,
            model=model,
            language=raw.get("language", "zh"),
            host=raw.get("host", "127.0.0.1"),
            port=int(raw.get("port", 8765)),
            keep_temporary_media=bool(raw.get("keep_temporary_media", False)),
            model_cache_dir=cache,
        )

    @property
    def output_dir(self) -> Path:
        return self.direct_output_dir or self.vault_dir / self.notes_subdir


def validate_notes_subdir(value: str) -> str:
    value = value.strip().replace("\\", "/")
    if not value:
        raise ValueError("笔记子目录不能为空")
    path = Path(value)
    if path.is_absolute() or any(part in {"..", "."} for part in path.parts):
        raise ValueError("笔记子目录必须是 Vault 内的相对路径")
    if any(re.search(r'[<>:"|?*\x00-\x1f]', part) for part in path.parts):
        raise ValueError("笔记子目录包含 Windows 不允许的字符")
    return str(path).replace("\\", "/")


def validate_output_dir(value: str) -> Path:
    path = Path(os.path.expandvars(value.strip())).expanduser()
    if not path.is_absolute():
        raise ValueError("保存文件夹必须是绝对路径")
    return path.resolve()


def output_dir_is_writable_or_creatable(path: Path) -> bool:
    if path.exists():
        return path.is_dir() and os.access(path, os.W_OK)
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK)


def select_vault_directory(initial_dir: str = "") -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            parent=root,
            initialdir=initial_dir or None,
            mustexist=True,
            title="选择 Obsidian Vault",
        )
        return selected or None
    finally:
        root.destroy()


def find_active_obsidian_vault(config_path: Path | None = None) -> Path | None:
    if config_path is None:
        app_data = os.environ.get("APPDATA", "").strip()
        if not app_data:
            return None
        config_path = Path(app_data) / "obsidian" / "obsidian.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    vaults = raw.get("vaults", {}) if isinstance(raw, dict) else {}
    if not isinstance(vaults, dict):
        return None

    valid: list[tuple[Path, bool, float]] = []
    for entry in vaults.values():
        if not isinstance(entry, dict):
            continue
        raw_path = str(entry.get("path", "")).strip()
        if not raw_path:
            continue
        vault = Path(os.path.expandvars(raw_path)).expanduser()
        if vault.is_dir() and (vault / ".obsidian").is_dir():
            try:
                last_used = float(entry.get("ts", float("-inf")))
            except (TypeError, ValueError):
                last_used = float("-inf")
            valid.append((vault.resolve(), entry.get("open") is True, last_used))

    open_vaults = [vault for vault, is_open, _last_used in valid if is_open]
    if len(open_vaults) == 1:
        return open_vaults[0]
    if not valid:
        return None
    most_recent_timestamp = max(last_used for _vault, _is_open, last_used in valid)
    most_recent = [
        vault for vault, _is_open, last_used in valid if last_used == most_recent_timestamp
    ]
    if most_recent_timestamp != float("-inf") and len(most_recent) == 1:
        return most_recent[0]
    all_vaults = [vault for vault, _is_open, _last_used in valid]
    return all_vaults[0] if len(all_vaults) == 1 else None


class ConfigManager:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.lock = threading.RLock()
        self.selection_lock = threading.Lock()
        self._raw = self._read()

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取配置文件：{exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("配置文件必须包含 JSON 对象")
        return raw

    def bootstrap_address(self) -> tuple[str, int]:
        with self.lock:
            return str(self._raw.get("host", "127.0.0.1")), int(self._raw.get("port", 8765))

    def _settings(self) -> Settings:
        return Settings.from_raw(self._raw, self.path)

    def require_settings(self) -> Settings:
        with self.lock:
            settings = self._settings()
            if settings.output_dir.exists() and not settings.output_dir.is_dir():
                raise ValueError("保存路径不是文件夹")
            if not output_dir_is_writable_or_creatable(settings.output_dir):
                raise ValueError("保存文件夹不可写，请检查权限")
            return settings

    def is_configured(self) -> bool:
        try:
            self.require_settings()
            return True
        except (ValueError, OSError):
            return False

    def public_settings(self) -> dict[str, Any]:
        with self.lock:
            raw_vault = str(self._raw.get("vault_dir", "")).strip()
            placeholder = not raw_vault or bool(
                re.search(r"Path[\\/]To[\\/]Your", raw_vault, re.IGNORECASE)
            )
            vault = None if placeholder else Path(os.path.expandvars(raw_vault)).expanduser()
            if vault is not None and not vault.is_absolute():
                vault = (self.path.parent / vault).resolve()
            exists = bool(vault and vault.is_dir())
            writable = bool(exists and os.access(vault, os.W_OK))
            notes_subdir = str(self._raw.get("notes_subdir", "课程笔记"))
            model = str(self._raw.get("model", "small"))
            raw_output_dir = str(self._raw.get("output_dir", "")).strip()
            output_dir = None
            try:
                if raw_output_dir:
                    output_dir = validate_output_dir(raw_output_dir)
                    fields_valid = model in ALLOWED_MODELS
                else:
                    validate_notes_subdir(notes_subdir)
                    output_dir = vault / notes_subdir if vault else None
                    fields_valid = model in ALLOWED_MODELS
            except ValueError:
                fields_valid = False
            output_writable = bool(output_dir and output_dir_is_writable_or_creatable(output_dir))
            return {
                "vault_dir": str(vault) if vault else "",
                "notes_subdir": notes_subdir,
                "output_dir": str(output_dir) if output_dir else "",
                "model": model,
                "keep_temporary_media": bool(self._raw.get("keep_temporary_media", False)),
                "configured": output_writable and fields_valid,
                "vault_exists": exists,
                "vault_writable": writable,
                "looks_like_vault": bool(exists and (vault / ".obsidian").is_dir()),
            }

    def _save(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(self._raw, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {"notes_subdir", "output_dir", "model", "keep_temporary_media"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError("不允许修改配置项：" + ", ".join(sorted(unknown)))
        if "output_dir" in values:
            output_dir = validate_output_dir(str(values["output_dir"]))
            if output_dir.exists() and not output_dir.is_dir():
                raise ValueError("保存路径不是文件夹")
            if not output_dir_is_writable_or_creatable(output_dir):
                raise ValueError("保存文件夹不可写，请检查权限")
            with self.lock:
                self._raw["output_dir"] = str(output_dir)
                self._save()
                return self.public_settings()
        notes_subdir = validate_notes_subdir(str(values.get("notes_subdir", self._raw.get("notes_subdir", "课程笔记"))))
        model = values.get("model", self._raw.get("model", "small"))
        if model not in ALLOWED_MODELS:
            raise ValueError(f"不支持的 Whisper 模型：{model}")
        keep_media = values.get(
            "keep_temporary_media", self._raw.get("keep_temporary_media", False)
        )
        if not isinstance(keep_media, bool):
            raise ValueError("保留临时视频必须是布尔值")
        with self.lock:
            self._raw.update(
                {
                    "notes_subdir": notes_subdir,
                    "model": model,
                    "keep_temporary_media": keep_media,
                }
            )
            self._save()
            return self.public_settings()

    def select_vault(self) -> dict[str, Any]:
        if not self.selection_lock.acquire(blocking=False):
            raise RuntimeError("文件夹选择窗口已经打开")
        try:
            current = self.public_settings()["vault_dir"]
            selected = select_vault_directory(current)
            if not selected:
                return {**self.public_settings(), "cancelled": True}
            vault = Path(selected).resolve()
            if not vault.is_dir():
                raise ValueError("选择的文件夹不存在")
            if not os.access(vault, os.W_OK):
                raise ValueError("选择的文件夹不可写")
            with self.lock:
                self._raw["vault_dir"] = str(vault)
                self._save()
            return {**self.public_settings(), "cancelled": False}
        finally:
            self.selection_lock.release()

    def auto_select_vault(self, obsidian_config: Path | None = None) -> dict[str, Any]:
        current = self.public_settings()
        if current["configured"]:
            return {**current, "auto_selected": False}
        vault = find_active_obsidian_vault(obsidian_config)
        if vault is None or not os.access(vault, os.W_OK):
            return {**current, "auto_selected": False}
        with self.lock:
            self._raw["vault_dir"] = str(vault)
            self._save()
        return {**self.public_settings(), "auto_selected": True}

    def test_write(self) -> dict[str, Any]:
        settings = self.require_settings()
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        output = settings.output_dir / "Tongji Course Clipper 测试.md"
        output.write_text(
            "# Tongji Course Clipper 测试\n\n"
            f"设置写入成功。\n\n- 插件版本：{VERSION}\n"
            f"- 测试时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n",
            encoding="utf-8",
        )
        return {"ok": True, "output": str(output)}


def compact_whitespace(value: str) -> str:
    text = T2S.convert((value or "").replace("\ufffd", ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace(",", "，").replace("?", "？").replace("!", "！")


def safe_filename(value: str) -> str:
    value = compact_whitespace(value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = value.rstrip(". ")
    return value[:180] or "未命名课程"


def validate_payload(raw: dict[str, Any]) -> dict[str, str]:
    required = ["page_url", "video_url", "title"]
    missing = [key for key in required if not compact_whitespace(str(raw.get(key, "")))]
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))

    page = urlparse(str(raw["page_url"]))
    video = urlparse(str(raw["video_url"]))
    if page.scheme != "https" or page.hostname != "look.tongji.edu.cn":
        raise ValueError("page_url must be an HTTPS look.tongji.edu.cn URL")
    if video.scheme != "https" or video.hostname != "look.tongji.edu.cn":
        raise ValueError("video_url must be an HTTPS look.tongji.edu.cn URL")

    return {
        "page_url": str(raw["page_url"]),
        "video_url": str(raw["video_url"]),
        "title": compact_whitespace(str(raw["title"])),
        "course_name": compact_whitespace(str(raw.get("course_name", ""))),
        "session": compact_whitespace(str(raw.get("session", ""))),
        "author": compact_whitespace(str(raw.get("author", ""))),
        "published": compact_whitespace(str(raw.get("published", ""))),
        "room": compact_whitespace(str(raw.get("room", ""))),
        "class_time": compact_whitespace(str(raw.get("class_time", ""))),
    }


def download_video(video_url: str, page_url: str, destination: Path) -> None:
    headers = {
        "Referer": page_url,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"
        ),
    }

    probe = urllib.request.Request(video_url, headers={**headers, "Range": "bytes=0-0"})
    with urllib.request.urlopen(probe, timeout=30) as response:
        content_range = response.headers.get("Content-Range", "")
    size_match = re.search(r"/(\d+)$", content_range)

    if size_match:
        total_size = int(size_match.group(1))
        chunk_size = 16 * 1024 * 1024
        ranges = [
            (start, min(start + chunk_size - 1, total_size - 1))
            for start in range(0, total_size, chunk_size)
        ]
        with destination.open("wb") as target:
            target.truncate(total_size)

        def fetch_range(byte_range: tuple[int, int]) -> None:
            start, end = byte_range
            for attempt in range(5):
                request = urllib.request.Request(
                    video_url,
                    headers={**headers, "Range": f"bytes={start}-{end}"},
                )
                try:
                    with urllib.request.urlopen(
                        request, timeout=60
                    ) as response, destination.open("r+b") as target:
                        if response.status != 206:
                            raise RuntimeError(f"range request returned HTTP {response.status}")
                        target.seek(start)
                        remaining = end - start + 1
                        while remaining:
                            block = response.read(min(1024 * 1024, remaining))
                            if not block:
                                raise RuntimeError(f"range {start}-{end} ended early")
                            target.write(block)
                            remaining -= len(block)
                    return
                except (TimeoutError, ConnectionError, urllib.error.URLError):
                    if attempt == 4:
                        raise
                    time.sleep(2**attempt)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(fetch_range, ranges))
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    else:
        request = urllib.request.Request(video_url, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as target:
            shutil.copyfileobj(response, target, length=1024 * 1024)
    if destination.stat().st_size == 0:
        raise RuntimeError("downloaded video is empty")


def transcribe_video(media_path: Path, metadata: dict[str, str], settings: Settings) -> list[dict[str, Any]]:
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    settings.model_cache_dir.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(
        settings.model,
        device="cpu",
        compute_type="int8",
        cpu_threads=os.cpu_count() or 4,
        download_root=str(settings.model_cache_dir),
    )
    pipeline = BatchedInferencePipeline(model=model)
    segments, _ = pipeline.transcribe(
        str(media_path),
        language=settings.language,
        beam_size=3,
        batch_size=4,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 700},
        condition_on_previous_text=False,
        without_timestamps=False,
        log_progress=False,
    )

    result = []
    previous = ""
    for segment in segments:
        text = compact_whitespace(segment.text)
        if not text:
            continue
        normalized = re.sub(r"\W+", "", text).lower()
        if normalized == previous and segment.end - segment.start < 20:
            continue
        if segment.no_speech_prob > 0.65 and segment.avg_logprob < -0.8:
            continue
        result.append(
            {
                "start": float(segment.start),
                "end": float(segment.end),
                "text": text,
            }
        )
        previous = normalized
    if not result:
        raise RuntimeError("speech recognition produced no transcript")
    return result


def merge_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for item in segments:
        if current is None:
            current = dict(item)
            continue
        duration = item["end"] - current["start"]
        gap = item["start"] - current["end"]
        length = len(current["text"]) + len(item["text"])
        if duration <= 32 and gap <= 3.5 and length <= 240:
            joiner = "" if re.search(r"[。！？；：]$", current["text"]) else "，"
            current["text"] += joiner + item["text"]
            current["end"] = item["end"]
        else:
            merged.append(current)
            current = dict(item)
    if current is not None:
        merged.append(current)
    return merged


def format_time(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_markdown(metadata: dict[str, str], segments: list[dict[str, Any]]) -> str:
    title = metadata["title"]
    course_name = metadata.get("course_name") or title
    description = f"{course_name}课程回放的自动转写笔记，由本地 Whisper 语音识别生成。"
    source = metadata["page_url"]
    author = metadata.get("author", "")
    published = metadata.get("published", "")
    details = " · ".join(
        part for part in [author, metadata.get("room", ""), metadata.get("class_time", "")] if part
    )
    lines = [
        "---",
        f"title: {yaml_string(title)}",
        f"source: {yaml_string(source)}",
    ]
    if author:
        lines.extend(["author:", f"  - {yaml_string(f'[[{author}]]')}"])
    if published:
        lines.append(f"published: {published}")
    lines.extend(
        [
            f"created: {time.strftime('%Y-%m-%d')}",
            f"description: {yaml_string(description)}",
            "tags:",
            '  - "clippings"',
            '  - "同济课程"',
            "---",
            (
                '<iframe width="100%" height="480" src="'
                + html.escape(source, quote=True)
                + '" title="同济课程回放" frameborder="0" allowfullscreen=""></iframe>'
            ),
            "",
            description,
        ]
    )
    if details:
        lines.extend(["", f"> {details}"])
    lines.extend(
        [
            "",
            "> [!note] 转写说明",
            "> 本文由本机 faster-whisper 自动转写；未调用 Agent、云端 LLM 或文本润色服务。专有名词、公式、代码和数值可能需要结合课程画面复核。",
            "",
            "## Transcript",
            "",
        ]
    )
    for item in merge_segments(segments):
        text = item["text"].strip()
        if text and not re.search(r"[。！？]$", text):
            text += "。"
        lines.extend([f"**{format_time(item['start'])}** · {text}", ""])
    return "\n".join(lines).rstrip() + "\n"


class JobManager:
    def __init__(
        self,
        settings: Settings | ConfigManager,
        state_path: Path | None = None,
    ):
        self.settings_source = settings
        self.lock = threading.Lock()
        if state_path is not None:
            self.state_path = state_path.resolve()
        elif isinstance(settings, ConfigManager):
            self.state_path = settings.path.with_name("jobs.json")
        else:
            self.state_path = None
        self.jobs = self._load_jobs()

    def _load_jobs(self) -> dict[str, dict[str, Any]]:
        if self.state_path is None or not self.state_path.is_file():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        jobs = {
            str(job_id): dict(job)
            for job_id, job in raw.items()
            if isinstance(job, dict) and job.get("id") == job_id
        }
        interrupted = False
        for job in jobs.values():
            if job.get("state") in ACTIVE_STATES:
                job.update(
                    state="failed",
                    error="本地服务重启导致任务中断，请重新提交",
                    completed_at=time.time(),
                )
                interrupted = True
        if interrupted:
            self.jobs = jobs
            self._save_locked()
        return jobs

    def _save_locked(self) -> None:
        if self.state_path is None:
            return
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        try:
            temporary.write_text(
                json.dumps(self.jobs, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def current_settings(self) -> Settings:
        if isinstance(self.settings_source, ConfigManager):
            return self.settings_source.require_settings()
        return self.settings_source

    def submit(self, raw: dict[str, Any]) -> dict[str, Any]:
        settings = self.current_settings()
        metadata = validate_payload(raw)
        with self.lock:
            for job in self.jobs.values():
                if job["page_url"] == metadata["page_url"] and job["state"] in ACTIVE_STATES:
                    return dict(job)
            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id,
                "state": "queued",
                "page_url": metadata["page_url"],
                "title": metadata["title"],
                "created_at": time.time(),
            }
            self.jobs[job_id] = job
            self._save_locked()
        threading.Thread(target=self._run, args=(job_id, metadata, settings), daemon=True).start()
        return dict(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            jobs = []
            for job in sorted(
                self.jobs.values(), key=lambda item: item.get("created_at", 0), reverse=True
            )[:50]:
                public_job = dict(job)
                public_job.pop("traceback", None)
                jobs.append(public_job)
            return jobs

    def _set(self, job_id: str, **values: Any) -> None:
        with self.lock:
            self.jobs[job_id].update(values)
            self._save_locked()

    def _run(self, job_id: str, metadata: dict[str, str], settings: Settings) -> None:
        work_dir: Path | None = None
        try:
            work_dir = Path(tempfile.mkdtemp(prefix="tongji-clipper-"))
            media_path = work_dir / "course.mp4"
            self._set(job_id, state="downloading")
            download_video(metadata["video_url"], metadata["page_url"], media_path)
            self._set(job_id, state="transcribing", downloaded_bytes=media_path.stat().st_size)
            segments = transcribe_video(media_path, metadata, settings)
            self._set(job_id, state="writing", segment_count=len(segments))
            settings.output_dir.mkdir(parents=True, exist_ok=True)
            output = settings.output_dir / f"{safe_filename(metadata['title'])}.md"
            output.write_text(render_markdown(metadata, segments), encoding="utf-8")
            self._set(job_id, state="completed", output=str(output), completed_at=time.time())
        except Exception as exc:
            self._set(
                job_id,
                state="failed",
                error=str(exc),
                traceback=traceback.format_exc(),
                completed_at=time.time(),
            )
        finally:
            if work_dir and work_dir.exists() and not settings.keep_temporary_media:
                shutil.rmtree(work_dir, ignore_errors=True)


class ApiHandler(BaseHTTPRequestHandler):
    manager: JobManager
    config_manager: ConfigManager

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin", "")
        return (
            not origin
            or origin == "null"
            or origin.startswith("chrome-extension://")
            or origin.startswith("edge-extension://")
            or origin.startswith("extension://")
            or origin.startswith("moz-extension://")
        )

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        origin = self.headers.get("Origin", "")
        if origin and self._origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _request_allowed(self) -> bool:
        origin = self.headers.get("Origin", "")
        return (
            (
                not origin
                or origin == "null"
                or origin.startswith("chrome-extension://")
                or origin.startswith("edge-extension://")
                or origin.startswith("extension://")
                or origin.startswith("moz-extension://")
            )
            and self.headers.get("X-Tongji-Clipper") == "1"
        )

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 64 * 1024:
            raise ValueError("invalid request size")
        raw = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("request body must be a JSON object")
        return raw

    def do_OPTIONS(self) -> None:
        if not self._origin_allowed():
            self._send(403, {"error": "origin not allowed"})
            return
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Tongji-Clipper")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._origin_allowed():
            self._send(403, {"error": "origin not allowed"})
            return
        if self.path == "/health":
            self._send(
                200,
                {
                    "app": APP_NAME,
                    "version": VERSION,
                    "status": "ok",
                    "configured": self.config_manager.is_configured(),
                },
            )
            return
        if not self._request_allowed():
            self._send(403, {"error": "request not allowed"})
            return
        if self.path == "/settings":
            self._send(200, self.config_manager.public_settings())
            return
        if self.path == "/jobs":
            self._send(200, self.manager.list())
            return
        match = re.fullmatch(r"/jobs/([a-f0-9]+)", self.path)
        if match:
            job = self.manager.get(match.group(1))
            self._send(200 if job else 404, job or {"error": "job not found"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._request_allowed():
            self._send(403, {"error": "request not allowed"})
            return
        if self.path == "/settings/auto-select-vault":
            try:
                self._send(200, self.config_manager.auto_select_vault())
            except OSError as exc:
                self._send(400, {"error": str(exc)})
            return
        if self.path == "/settings/select-vault":
            try:
                self._send(200, self.config_manager.select_vault())
            except RuntimeError as exc:
                self._send(409, {"error": str(exc)})
            except (ValueError, OSError) as exc:
                self._send(400, {"error": str(exc)})
            return
        if self.path == "/settings/test-write":
            try:
                self._send(200, self.config_manager.test_write())
            except (ValueError, OSError) as exc:
                self._send(400, {"error": str(exc)})
            return
        if self.path != "/jobs":
            self._send(404, {"error": "not found"})
            return
        try:
            raw = self._read_json()
            job = self.manager.submit(raw)
            self._send(202, job)
        except (ValueError, json.JSONDecodeError) as exc:
            status = 400 if self.config_manager.is_configured() else 409
            self._send(status, {"error": str(exc)})

    def do_PUT(self) -> None:
        if not self._request_allowed():
            self._send(403, {"error": "request not allowed"})
            return
        if self.path != "/settings":
            self._send(404, {"error": "not found"})
            return
        try:
            self._send(200, self.config_manager.update(self._read_json()))
        except (ValueError, json.JSONDecodeError, OSError) as exc:
            self._send(400, {"error": str(exc)})


def main() -> int:
    parser = argparse.ArgumentParser(description="Local backend for Tongji Course Clipper")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    args = parser.parse_args()
    if not args.config.is_file():
        parser.error(f"config file not found: {args.config}")
    config_manager = ConfigManager(args.config.resolve())
    host, port = config_manager.bootstrap_address()
    manager = JobManager(config_manager)
    ApiHandler.manager = manager
    ApiHandler.config_manager = config_manager
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print(f"{APP_NAME} {VERSION} listening on http://{host}:{port}")
    if config_manager.is_configured():
        print(f"Notes directory: {config_manager.require_settings().output_dir}")
    else:
        print("Obsidian Vault is not configured. Open the extension settings to continue.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
