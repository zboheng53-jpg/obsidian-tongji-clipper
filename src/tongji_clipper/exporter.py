"""Export transcripts to Markdown, SRT, and raw segments JSON with atomic file writing."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import threading
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def get_library_versions() -> Dict[str, str]:
    """Retrieves actual runtime library versions for provenance recording."""
    versions = {
        "python": sys.version.split()[0],
    }
    try:
        import faster_whisper
        versions["faster_whisper"] = getattr(faster_whisper, "__version__", "unknown")
    except Exception:
        versions["faster_whisper"] = "not_installed"

    try:
        import ctranslate2
        versions["ctranslate2"] = getattr(ctranslate2, "__version__", "unknown")
    except Exception:
        versions["ctranslate2"] = "not_installed"

    return versions


def format_seconds_to_hms(seconds: float) -> str:
    """Formats seconds to HH:MM:SS string."""
    s = int(round(seconds))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


def format_seconds_to_srt_time(seconds: float) -> str:
    """Formats seconds to HH:MM:SS,mmm for SubRip subtitles."""
    millis = int(round(seconds * 1000))
    h = millis // 3600000
    m = (millis % 3600000) // 60000
    s = (millis % 60000) // 1000
    ms = millis % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_markdown(
    job_info: Dict[str, Any],
    segments: List[Dict[str, Any]],
    timings: Dict[str, float],
) -> str:
    """
    Builds Markdown content with JSON-escaped YAML frontmatter, wikilink,
    and #clip-t= timestamp links.
    """
    config = job_info.get("config", {})
    title = job_info.get("course_title") or job_info.get("sub_title") or "课程逐字稿"
    sub_title = job_info.get("sub_title") or ""
    lecturer = job_info.get("lecturer_name") or "未知教师"
    room = job_info.get("room_name") or "未知教室"
    source_url = job_info.get("source_url") or ""
    tenant_code = job_info.get("tenant_code") or ""
    course_id = job_info.get("course_id") or ""
    sub_id = job_info.get("sub_id") or ""

    now_iso = datetime.now(timezone.utc).isoformat()
    lib_vers = get_library_versions()

    # Frontmatter with safely serialized JSON values to prevent YAML formatting bugs
    lines = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"sub_title: {json.dumps(sub_title, ensure_ascii=False)}",
        f"tenant_code: {json.dumps(tenant_code, ensure_ascii=False)}",
        f"course_id: {json.dumps(course_id, ensure_ascii=False)}",
        f"sub_id: {json.dumps(sub_id, ensure_ascii=False)}",
        f"lecturer_name: {json.dumps(lecturer, ensure_ascii=False)}",
        f"room_name: {json.dumps(room, ensure_ascii=False)}",
        f"source_url: {json.dumps(source_url, ensure_ascii=False)}",
        f"created_at: {json.dumps(now_iso, ensure_ascii=False)}",
        f"model: {json.dumps(config.get('model', 'small'), ensure_ascii=False)}",
        f"model_version: {json.dumps(lib_vers.get('faster_whisper', '1.0.0'), ensure_ascii=False)}",
        f"device: {json.dumps(config.get('device', 'cpu'), ensure_ascii=False)}",
        f"compute_type: {json.dumps(config.get('compute_type', 'int8'), ensure_ascii=False)}",
        f"offline: {json.dumps(bool(config.get('offline', False)))}",
        f"asr_revision: {json.dumps(config.get('asr_revision', 1))}",
        f"hotwords: {json.dumps(config.get('hotwords', ''), ensure_ascii=False)}",
        f"acquisition_seconds: {timings.get('acquisition', 0.0):.2f}",
        f"asr_seconds: {timings.get('asr', 0.0):.2f}",
        f"export_seconds: {timings.get('export', 0.0):.2f}",
        f"total_elapsed_seconds: {timings.get('total', 0.0):.2f}",
        "---",
        "",
        f"# {title}" + (f" - {sub_title}" if sub_title and sub_title != title else ""),
        "",
        f"- **教师**：[[{lecturer}]]",
        f"- **教室**：{room}",
        f"- **来源**：[{source_url}]({source_url})",
        "",
        "---",
        "",
    ]

    for seg in segments:
        start_sec = seg["start"]
        hms = format_seconds_to_hms(start_sec)
        t_int = int(round(start_sec))
        if source_url:
            link_target = f"{source_url}#clip-t={t_int}"
        else:
            link_target = f"#clip-t={t_int}"

        text = seg["text"].strip()
        lines.append(f"[{hms}]({link_target}) {text}\n")

    return "\n".join(lines)


def build_srt(segments: List[Dict[str, Any]]) -> str:
    """Builds SubRip format subtitle text."""
    blocks = []
    for idx, seg in enumerate(segments, start=1):
        start_str = format_seconds_to_srt_time(seg["start"])
        end_str = format_seconds_to_srt_time(seg["end"])
        text = seg["text"].strip()
        blocks.append(f"{idx}\n{start_str} --> {end_str}\n{text}\n")
    return "\n".join(blocks)


def write_atomic(target_path: Path, content: str) -> None:
    """Writes content to a temporary file in target directory, then replaces atomically."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f"{target_path.name}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, target_path)


def resolve_non_colliding_path(directory: Path, base_name: str, ext: str) -> Path:
    """Finds next available revision path if target already exists."""
    candidate = directory / f"{base_name}.{ext}"
    if not any((directory / f"{candidate.stem}.{suffix}").exists() for suffix in ('md', 'srt', 'segments.json', 'raw.json')):
        return candidate

    rev = 1
    while True:
        candidate = directory / f"{base_name}_rev{rev}.{ext}"
        if not any((directory / f"{candidate.stem}.{suffix}").exists() for suffix in ('md', 'srt', 'segments.json', 'raw.json')):
            return candidate
        rev += 1


_export_lock = threading.RLock()


def export_job_results(job_info, segments, output_dir, timings):
    # The HTTP server can receive exports from multiple windows at once.
    with _export_lock:
        return _export_job_results(job_info, segments, output_dir, timings)


def _export_job_results(
    job_info: Dict[str, Any],
    segments: List[Dict[str, Any]],
    output_dir: Path,
    timings: Dict[str, float],
) -> Dict[str, str]:
    """
    Exports Markdown, SRT, and raw segments JSON atomically without overwriting user files.
    Returns dictionary with generated file paths.
    """
    export_started = time.perf_counter()
    timings = dict(timings)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise OSError(f"Cannot create output directory {output_dir}: {e}") from e

    base_name = f"{job_info.get('course_id', 'course')}_{job_info.get('sub_id', 'sub')}"
    clean_base = re.sub(r'[\\/*?:"<>|]', "_", base_name)

    md_path = resolve_non_colliding_path(output_dir, clean_base, "md")
    # Matching base name for subtitle & JSON
    base_stem = md_path.stem
    srt_path = output_dir / f"{base_stem}.srt"
    json_path = output_dir / f"{base_stem}.segments.json"

    # 1. Markdown
    md_content = build_markdown(job_info, segments, timings)

    # 2. SRT
    srt_content = build_srt(segments)
    write_atomic(srt_path, srt_content)

    # 3. Segments JSON
    json_content = json.dumps(segments, ensure_ascii=False, indent=2)
    write_atomic(json_path, json_content)

    checkpoint = output_dir / 'checkpoint.json'
    raw_path = output_dir / f'{base_stem}.raw.json'
    if checkpoint.is_file():
        raw = json.loads(checkpoint.read_text(encoding='utf-8'))
        write_atomic(raw_path, json.dumps({'raw_chunks': raw['raw_chunks'], 'config': job_info.get('config', {}), 'library_versions': get_library_versions()}, ensure_ascii=False, indent=2))

    timings['export'] = time.perf_counter() - export_started
    timings['total'] = timings.get('total', 0) + timings['export']
    write_atomic(md_path, build_markdown(job_info, segments, timings))

    result_paths = {
        "markdown": str(md_path),
        "srt": str(srt_path),
        "segments": str(json_path),
    }
    if checkpoint.is_file():
        result_paths['raw'] = str(raw_path)

    # Verify all artifacts
    verify_exported_artifacts(result_paths)
    return result_paths


def verify_exported_artifacts(paths: Dict[str, str]) -> None:
    """Verifies that all exported files exist, are non-empty, and JSON parses."""
    for key, p_str in paths.items():
        p = Path(p_str)
        if not p.is_file():
            raise RuntimeError(f"Export verification failed: {key} file not created ({p_str})")
        if p.stat().st_size <= 0 and key != 'srt':
            raise RuntimeError(f"Export verification failed: {key} file is empty ({p_str})")

    # Validate JSON integrity
    with open(paths["segments"], "r", encoding="utf-8") as f:
        data = json.load(f)
        if not isinstance(data, list):
            raise RuntimeError(f"Export verification failed: segments.json is not a JSON list")


def cleanup_temporary_audio(audio_path: Optional[str], verification_paths: Optional[Dict[str, str]] = None) -> None:
    """
    Safely cleans up only the temporary extracted WAV file.
    Only proceeds if exported artifacts verify successfully!
    """
    if not audio_path:
        return

    if verification_paths is not None:
        verify_exported_artifacts(verification_paths)

    p = Path(audio_path)
    if p.is_file() and p.suffix.lower() == ".wav":
        try:
            p.unlink()
            logger.info("Cleaned up temporary audio: %s", audio_path)
        except OSError as e:
            logger.warning("Could not delete temporary audio %s: %s", audio_path, e)
