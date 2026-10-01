"""Audio extraction and validation using FFmpeg and FFprobe without shell execution."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from tongji_clipper.config import sanitize_url, redact_urls

logger = logging.getLogger(__name__)


class AcquisitionError(Exception):
    """Base exception for audio acquisition failures."""
    pass


class SignatureExpiredError(AcquisitionError):
    """Raised when remote media stream URL returns 401/403 or signature invalid."""
    pass


def find_binary(binary_name: str) -> str:
    """Finds binary path or raises AcquisitionError if not found in PATH."""
    path = shutil.which(binary_name)
    if not path:
        raise AcquisitionError(f"Required binary '{binary_name}' not found in PATH. Please install it.")
    return path


def probe_audio(file_path: str) -> Dict[str, float]:
    """
    Validates the generated 16kHz mono PCM WAV file with ffprobe.
    Verifies codec is strictly pcm_s16le, sample_rate 16000, 1 channel, duration > 0.
    """
    ffprobe_bin = find_binary("ffprobe")
    p = Path(file_path)
    if not p.is_file():
        raise AcquisitionError(f"Audio file does not exist: {file_path}")

    args = [
        ffprobe_bin,
        "-v", "error",
        "-show_entries", "format=duration,size:stream=codec_name,codec_type,channels,sample_rate",
        "-of", "json",
        str(p.resolve()),
    ]

    try:
        res = subprocess.run(args, capture_output=True, text=True, check=True)
        info = json.loads(res.stdout)
    except subprocess.CalledProcessError as e:
        raise AcquisitionError(f"ffprobe failed: {e.stderr}") from e
    except json.JSONDecodeError as e:
        raise AcquisitionError(f"Failed to parse ffprobe json output: {e}") from e

    streams = info.get("streams", [])
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio_streams:
        raise AcquisitionError("No audio stream detected in output file")

    a_stream = audio_streams[0]
    codec_name = a_stream.get("codec_name", "")
    sample_rate = int(a_stream.get("sample_rate", 0))
    channels = int(a_stream.get("channels", 0))

    fmt = info.get("format", {})
    duration = float(fmt.get("duration", 0.0))
    size = int(fmt.get("size", 0))

    if codec_name != "pcm_s16le":
        raise AcquisitionError(f"Audio codec is '{codec_name}', expected 'pcm_s16le'")
    if duration <= 0:
        raise AcquisitionError(f"Extracted audio has invalid duration: {duration}s")
    if sample_rate != 16000:
        raise AcquisitionError(f"Audio sample rate is {sample_rate}Hz, expected 16000Hz")
    if channels != 1:
        raise AcquisitionError(f"Audio channels count is {channels}, expected 1 (mono)")

    return {
        "duration": duration,
        "sample_rate": sample_rate,
        "channels": channels,
        "size": size,
    }


def extract_pcm_wav(
    source_url_or_path: str,
    output_wav_path: str,
    check_cancelled: Optional[Callable[[], bool]] = None,
    process_holder: Optional[List[subprocess.Popen]] = None,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    total_duration: Optional[float] = None,
) -> Dict[str, float]:
    """
    Extracts 16kHz mono 16-bit PCM WAV from source_url_or_path using ffmpeg.
    - Uses argument list without shell (shell=False).
    - Uses -nostdin, -map 0:a:0, -rw_timeout 15000000.
    - Reads stdout asynchronously for -progress pipe:1 to avoid pipe deadlock.
    - Writes stderr to temporary file for diagnosis without pipe buffer deadlock.
    - Sanitizes all log messages and exception strings to never leak signed URLs.
    """
    ffmpeg_bin = find_binary("ffmpeg")
    out_path = Path(output_wav_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sanitized_source = sanitize_url(source_url_or_path)

    args = [
        ffmpeg_bin,
        "-nostdin",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-rw_timeout", "15000000",
        "-progress", "pipe:1",
        "-i", source_url_or_path,
        "-vn",
        "-map", "0:a:0",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-f", "wav",
        str(out_path),
    ]

    logger.info("Executing ffmpeg: %s (source: %s)", ffmpeg_bin, sanitized_source)

    # Use a temporary file for stderr to prevent pipe buffer deadlock
    stderr_file = tempfile.NamedTemporaryFile(mode="w+", delete=False, encoding="utf-8", errors="replace")
    stderr_file_path = Path(stderr_file.name)

    proc: Optional[subprocess.Popen] = None
    start_time = time.time()
    progress_state = {
        "audio_seconds": 0.0,
        "speed": "0x",
        "last_activity": start_time,
    }

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            errors="replace",
        )

        if process_holder is not None:
            process_holder.clear()
            process_holder.append(proc)

        def read_stdout():
            try:
                assert proc is not None and proc.stdout is not None
                for line in iter(proc.stdout.readline, ""):
                    if not line:
                        break
                    line = line.strip()
                    if "=" in line:
                        k, v = line.split("=", 1)
                        now = time.time()
                        progress_state["last_activity"] = now
                        if k in ("out_time_us", "out_time_ms"):
                            try:
                                # Both out_time_us and out_time_ms in ffmpeg -progress are in MICROSECONDS
                                progress_state["audio_seconds"] = max(0.0, float(v) / 1_000_000.0)
                            except ValueError:
                                pass
                        elif k == "out_time":
                            try:
                                parts = v.strip().split(":")
                                if len(parts) == 3:
                                    h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
                                    progress_state["audio_seconds"] = max(0.0, h * 3600.0 + m * 60.0 + s)
                            except ValueError:
                                pass
                        elif k == "speed":
                            progress_state["speed"] = v
                        elif k == "progress":
                            audio_sec = progress_state["audio_seconds"]
                            spd = progress_state["speed"]
                            pct = None
                            if total_duration and total_duration > 0:
                                pct = round(min(100.0, (audio_sec / total_duration) * 100.0), 1)
                            if on_progress:
                                elapsed_sec = round(now - start_time, 1)
                                on_progress({
                                    "stage": "acquisition",
                                    "audio_seconds": round(audio_sec, 1),
                                    "processed_seconds": round(audio_sec, 1),
                                    "elapsed_seconds": elapsed_sec,
                                    "speed": spd,
                                    "percent": pct,
                                    "last_activity": progress_state["last_activity"],
                                    "message": f"正在提取音频: {audio_sec:.1f}s ({spd})",
                                })
            except Exception:
                pass

        reader_thread = threading.Thread(target=read_stdout, daemon=True)
        reader_thread.start()

        # Monitor loop
        while proc.poll() is None:
            if check_cancelled and check_cancelled():
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                reader_thread.join(timeout=1.0)
                stderr_file.close()
                stderr_file_path.unlink(missing_ok=True)
                raise AcquisitionError("Acquisition cancelled by user")
            time.sleep(0.3)

        reader_thread.join(timeout=2.0)

    finally:
        stderr_file.close()
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass
        if process_holder is not None and proc in process_holder:
            process_holder.remove(proc)

    # Read captured stderr
    stderr_content = ""
    if stderr_file_path.is_file():
        try:
            stderr_content = stderr_file_path.read_text(encoding="utf-8", errors="replace")
        finally:
            try:
                stderr_file_path.unlink()
            except OSError:
                pass

    if proc is not None and proc.returncode != 0:
        lower_err = stderr_content.lower()
        if "403 forbidden" in lower_err or "http error 403" in lower_err or "401 unauthorized" in lower_err or "invalid signature" in lower_err:
            raise SignatureExpiredError(
                f"Media stream signature expired or forbidden (HTTP 401/403) for source: {sanitized_source}"
            )
        raise AcquisitionError(
            f"ffmpeg extraction failed (code {proc.returncode}) for {sanitized_source}: {redact_urls(stderr_content.strip())}"
        )

    # Validate output with ffprobe
    return probe_audio(str(out_path))
