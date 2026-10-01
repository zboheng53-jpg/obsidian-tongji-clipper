"""Localhost HTTP server for Tongji Clipper background worker daemon."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import subprocess
import sys
import socket
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from tongji_clipper.config import (
    generate_auth_token,
    get_app_dir,
    get_default_output_dir,
    get_models_cache_dir,
    load_worker_info,
    remove_worker_info,
    save_worker_info,
)
from tongji_clipper.db import Database
from tongji_clipper.models import CapturePayload, JobConfig
from tongji_clipper.worker.runner import JobRunner

logger = logging.getLogger(__name__)

MAX_BODY_SIZE = 5 * 1024 * 1024  # 5 MiB protection


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if not (PROJECT_ROOT / "src").is_dir():
    PROJECT_ROOT = Path.cwd()


def is_complete_model_dir(path: Path) -> bool:
    """Verifies that a directory contains complete Faster-Whisper model assets."""
    if not path.is_dir():
        return False

    def _check_dir(d: Path) -> bool:
        if not d.is_dir():
            return False
        # Must have CTranslate2 model.bin (> 50KB to reject empty or fake KB files)
        has_model = False
        for name in ("model.bin",):
            f = d / name
            if f.is_file() and f.stat().st_size > 50 * 1024:
                has_model = True
                break
        if not has_model:
            return False

        # Must have config.json (> 0 bytes)
        config_f = d / "config.json"
        if not (config_f.is_file() and config_f.stat().st_size > 0):
            return False

        # Must have tokenizer.json or vocabulary.json/txt (> 0 bytes)
        has_tokenizer = any(
            (d / name).is_file() and (d / name).stat().st_size > 0
            for name in ("tokenizer.json", "vocabulary.json", "vocabulary.txt")
        )
        return has_tokenizer

    if _check_dir(path):
        return True

    # Check snapshots subdirectory (for huggingface cache format)
    snapshots_dir = path / "snapshots"
    if snapshots_dir.is_dir():
        for snap in snapshots_dir.iterdir():
            if snap.is_dir() and _check_dir(snap):
                return True

    return False


class WorkerRequestHandler(BaseHTTPRequestHandler):
    server: WorkerServer  # Type hint

    def setup(self) -> None:
        super().setup()
        self.timeout = 10.0  # 10s socket timeout
        self.connection.settimeout(self.timeout)

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)

    def _send_json(self, status_code: int, data: Dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        auth_header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        if auth_header != expected:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "Unauthorized: invalid or missing bearer token"},
            )
            return False
        return True

    def _read_json_body(self) -> Optional[Any]:
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0:
                return {}
            if content_length > MAX_BODY_SIZE:
                self.close_connection = True
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Payload exceeds 5MB limit"})
                self.wfile.flush()
                # On Windows closing with unread request bytes can reset the
                # socket before the client receives the rejection body.
                self.connection.shutdown(socket.SHUT_WR)
                remaining = min(content_length, MAX_BODY_SIZE * 2)
                deadline = time.monotonic() + 1.0
                self.connection.settimeout(0.2)
                try:
                    while remaining and time.monotonic() < deadline:
                        chunk = self.rfile.read1(min(65536, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                except OSError:
                    pass
                return None
            raw_body = self.rfile.read(content_length)
            return json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid JSON body: {e}"})
            return None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "pid": os.getpid()})
            return

        if not self._check_auth():
            return

        if path == "/system_info":
            # Discover local models using stable PROJECT_ROOT or HF cache
            proj_model = PROJECT_ROOT / "models" / "faster-whisper-small"
            cache_model = get_models_cache_dir() / "models--Systran--faster-whisper-small"
            detected_model_path: Optional[Path] = None
            if is_complete_model_dir(proj_model):
                detected_model_path = proj_model.resolve()
                m_msg = f"本地项目内置模型已存在: {detected_model_path}"
            elif is_complete_model_dir(cache_model):
                snaps_dir = cache_model / "snapshots"
                valid_snaps = [s for s in snaps_dir.iterdir() if s.is_dir() and is_complete_model_dir(s)] if snaps_dir.is_dir() else []
                if valid_snaps:
                    detected_model_path = valid_snaps[0].resolve()
                else:
                    detected_model_path = cache_model.resolve()
                m_msg = f"本地缓存模型已存在: {detected_model_path}"
            else:
                m_msg = "未安装本地模型（不默认承诺离线可用）"

            model_ok = detected_model_path is not None
            defaults = {
                "model": str(detected_model_path) if detected_model_path else "small",
                "offline": model_ok,
                "output_dir": str(get_default_output_dir()),
                "resource_profile": "balanced",
            }
            diagnostics = []
            ff = shutil.which("ffmpeg")
            diagnostics.append({"name": "ffmpeg", "ok": bool(ff), "message": f"FFmpeg 存在 ({ff})" if ff else "FFmpeg 未找到"})
            fp = shutil.which("ffprobe")
            diagnostics.append({"name": "ffprobe", "ok": bool(fp), "message": f"FFprobe 存在 ({fp})" if fp else "FFprobe 未找到"})
            diagnostics.append({"name": "model_local", "ok": model_ok, "message": m_msg})

            worker_state = {
                "runner_alive": self.server.runner.runner_alive,
                "last_heartbeat": self.server.runner.last_heartbeat,
            }
            cache_state = {
                "bytes": self.server.db.get_total_cache_bytes(),
            }
            self._send_json(HTTPStatus.OK, {
                "status": "ok",
                "defaults": defaults,
                "diagnostics": diagnostics,
                "worker": worker_state,
                "cache": cache_state,
            })
            return

        if path == "/jobs":
            qs = parse_qs(parsed.query)
            limit: Optional[int] = None
            offset: int = 0
            try:
                if "limit" in qs:
                    limit = int(qs["limit"][0])
                    if limit < 0:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "limit must be non-negative"})
                        return
                if "offset" in qs:
                    offset = int(qs["offset"][0])
                    if offset < 0:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "offset must be non-negative"})
                        return
            except (ValueError, TypeError) as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid limit or offset: {e}"})
                return

            jobs = self.server.db.list_jobs(limit=limit, offset=offset)
            self._send_json(HTTPStatus.OK, {"jobs": jobs})
            return

        if path.startswith("/jobs/"):
            job_id = path[len("/jobs/"):]
            job = self.server.db.get_job(job_id, include_signature=False)
            if not job:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Job {job_id} not found"})
                return
            self._send_json(HTTPStatus.OK, {"job": job})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Endpoint not found: {path}"})

    def do_POST(self) -> None:
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        body = self._read_json_body()
        if body is None:
            return
        if not isinstance(body, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be a JSON object"})
            return

        if path == "/submit":
            try:
                capture_data = body.get("capture")
                if not capture_data:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'capture' in body"})
                    return
                capture = CapturePayload.from_dict(capture_data)
                capture.validate()

                stream_id = body.get("stream_id", "")
                cfg_data = body.get("config", {})
                config = JobConfig.from_dict(cfg_data)

                job = self.server.db.submit_job(capture, stream_id=stream_id, config=config)
                self._send_json(HTTPStatus.OK, {"status": "ok", "job": job})
                return
            except Exception as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Failed submitting job: {e}"})
                return

        if path == "/cancel":
            job_id = body.get("job_id")
            if not job_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'job_id'"})
                return
            ok = self.server.runner.cancel_job(job_id)
            if not ok:
                self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "error": f"Failed to cancel job {job_id}", "job_id": job_id})
                return
            self._send_json(HTTPStatus.OK, {"status": "ok", "cancelled": True, "job_id": job_id})
            return

        if path == "/pause":
            job_id = body.get("job_id")
            if not job_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'job_id'"})
                return
            ok = self.server.runner.pause_job(job_id)
            if not ok:
                self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "error": f"Failed to pause job {job_id}", "job_id": job_id})
                return
            self._send_json(HTTPStatus.OK, {"status": "ok", "paused": True, "job_id": job_id})
            return

        if path == "/resume":
            job_id = body.get("job_id")
            if not job_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'job_id'"})
                return
            try:
                job = self.server.runner.resume_job(job_id)
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Job {job_id} not found"})
                    return
                self._send_json(HTTPStatus.OK, {"status": "ok", "job": job})
                return
            except Exception as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Failed resuming job: {e}"})
                return

        if path == "/retry":
            job_id = body.get("job_id")
            if not job_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'job_id'"})
                return
            try:
                job = self.server.runner.retry_job(job_id)
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Job {job_id} not found"})
                    return
                self._send_json(HTTPStatus.OK, {"status": "ok", "job": job})
                return
            except Exception as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Failed retrying job: {e}"})
                return

        if path == "/refresh_signature":
            job_id = body.get("job_id")
            signed_url = body.get("signed_url")
            if not job_id or not signed_url:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'job_id' or 'signed_url'"})
                return
            try:
                job = self.server.db.refresh_signature(job_id, signed_url)
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Job {job_id} not found"})
                    return
                self._send_json(HTTPStatus.OK, {"status": "ok", "job": job})
                return
            except Exception as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Failed refreshing signature: {e}"})
                return

        if path == "/validate_config":
            cfg_data = body.get("config", {})
            if not isinstance(cfg_data, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Config must be a JSON object"})
                return
            try:
                cfg = JobConfig.from_dict(cfg_data)
            except Exception as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "error": f"Invalid config schema: {e}"})
                return

            checks = []
            ff = shutil.which("ffmpeg")
            checks.append({"name": "ffmpeg", "ok": bool(ff), "message": "FFmpeg 可用" if ff else "FFmpeg 未找到"})
            fp = shutil.which("ffprobe")
            checks.append({"name": "ffprobe", "ok": bool(fp), "message": "FFprobe 可用" if fp else "FFprobe 未找到"})

            # Model path / offline validity
            model_ok = True
            model_msg = "模型配置有效"
            model_path = Path(cfg.model)
            is_local_path = model_path.is_absolute() or "\\" in cfg.model or cfg.model.startswith(".")
            if is_local_path:
                candidate = model_path if model_path.is_absolute() else model_path.resolve()
                model_ok = is_complete_model_dir(candidate)
                model_msg = "本地模型文件完整" if model_ok else f"本地模型路径不存在或文件不完整: {candidate}"
            elif cfg.offline:
                cache_root = Path(cfg.model_dir) if cfg.model_dir else get_models_cache_dir()
                candidate = cache_root / f"models--Systran--faster-whisper-{cfg.model}"
                model_ok = is_complete_model_dir(candidate)
                model_msg = "离线模型缓存完整" if model_ok else f"预设模型离线缓存不存在或不完整: {candidate}；可改选本地模型绝对路径"
            else:
                model_msg = "在线预设模型：首次运行可能需要下载，未联网校验远端可用性"
            checks.append({"name": "model_offline", "ok": model_ok, "message": model_msg})

            # Output dir writability
            out_ok = True
            out_msg = "输出目录可写"
            target_out = Path(cfg.output_dir) if cfg.output_dir else get_default_output_dir()
            try:
                target_out.mkdir(parents=True, exist_ok=True)
                probe_file = target_out / f".probe_write_{os.getpid()}_{time.time_ns()}.tmp"
                probe_file.write_text("probe", encoding="utf-8")
                probe_file.unlink()
            except Exception as e:
                out_ok = False
                out_msg = f"输出路径不可写: {e}"
            checks.append({"name": "output_dir_writable", "ok": out_ok, "message": out_msg})

            all_ok = all(c["ok"] for c in checks)
            err = None if all_ok else "; ".join(c["message"] for c in checks if not c["ok"])
            code = HTTPStatus.OK if all_ok else HTTPStatus.BAD_REQUEST
            self._send_json(code, {
                "status": "ok" if all_ok else "error",
                "checks": checks,
                "error": err,
            })
            return

        if path == "/open_output":
            job_id = body.get("job_id")
            kind = body.get("kind", "folder")
            if not job_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'job_id'"})
                return
            job = self.server.db.get_job(job_id, include_signature=False)
            if not job or not job.get("output_dir"):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Job {job_id} or output_dir not found"})
                return

            out_dir = Path(job["output_dir"])
            exported_files = job.get("exported_files") or {}

            target: Optional[Path] = None
            if kind == "folder":
                target = out_dir
            elif kind == "markdown":
                if exported_files.get("markdown") and Path(exported_files["markdown"]).exists():
                    target = Path(exported_files["markdown"])
                else:
                    c_id = job.get("course_id", "")
                    s_id = job.get("sub_id", "")
                    prefix = f"{c_id}_{s_id}" if c_id and s_id else ""
                    if prefix:
                        matches = sorted(out_dir.glob(f"{prefix}*.md"))
                        if matches:
                            target = matches[0]
                    if not target and (out_dir / "transcript.md").exists():
                        target = out_dir / "transcript.md"
            elif kind == "srt":
                if exported_files.get("srt") and Path(exported_files["srt"]).exists():
                    target = Path(exported_files["srt"])
                else:
                    c_id = job.get("course_id", "")
                    s_id = job.get("sub_id", "")
                    prefix = f"{c_id}_{s_id}" if c_id and s_id else ""
                    if prefix:
                        matches = sorted(out_dir.glob(f"{prefix}*.srt"))
                        if matches:
                            target = matches[0]
                    if not target and (out_dir / "transcript.srt").exists():
                        target = out_dir / "transcript.srt"
            elif kind == "segments":
                if exported_files.get("segments") and Path(exported_files["segments"]).exists():
                    target = Path(exported_files["segments"])
                else:
                    c_id = job.get("course_id", "")
                    s_id = job.get("sub_id", "")
                    prefix = f"{c_id}_{s_id}" if c_id and s_id else ""
                    if prefix:
                        matches = sorted(out_dir.glob(f"{prefix}*.segments.json"))
                        if matches:
                            target = matches[0]
            elif kind == "raw":
                if exported_files.get("raw") and Path(exported_files["raw"]).exists():
                    target = Path(exported_files["raw"])
                else:
                    c_id = job.get("course_id", "")
                    s_id = job.get("sub_id", "")
                    prefix = f"{c_id}_{s_id}" if c_id and s_id else ""
                    if prefix:
                        matches = sorted(out_dir.glob(f"{prefix}*.raw.json"))
                        if matches:
                            target = matches[0]
            else:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid kind: '{kind}'"})
                return

            if not target or not target.exists():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Target {kind} does not exist at {target}"})
                return

            try:
                if os.name == "nt":
                    os.startfile(str(target))
                else:
                    subprocess.run(["xdg-open", str(target)], check=True)
            except Exception as e:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Failed opening {kind} at {target}: {e}"})
                return

            self._send_json(HTTPStatus.OK, {"status": "ok", "path": str(target)})
            return

        if path == "/export":
            job_id = body.get("job_id")
            output_dir = body.get("output_dir")
            keep_audio = bool(body.get("keep_audio", False))
            if not job_id or not output_dir:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'job_id' or 'output_dir'"})
                return
            try:
                res = self.server.db.export_job(job_id, output_dir, keep_audio=keep_audio)
                if not res.get("files"):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Export resulted in zero files", "details": res})
                    return
                self._send_json(HTTPStatus.OK, res)
                return
            except Exception as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Export failed: {e}"})
                return

        if path == "/cleanup_cache":
            job_id = body.get("job_id")
            try:
                freed = self.server.db.cleanup_cache(job_id=job_id)
                self._send_json(HTTPStatus.OK, {"status": "ok", "freed_bytes": freed})
                return
            except Exception as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Cleanup failed: {e}"})
                return

        if path == "/preview":
            job_id = body.get("job_id")
            try:
                max_lines = int(body.get("max_lines", 50))
                if max_lines <= 0:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "max_lines must be greater than 0"})
                    return
            except (ValueError, TypeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid max_lines"})
                return

            if not job_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'job_id'"})
                return
            job = self.server.db.get_job(job_id, include_signature=False)
            if not job or not job.get("output_dir"):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Job {job_id} or output_dir not found"})
                return

            out_dir = Path(job["output_dir"])
            source_url = job.get("source_url") or ""
            exported_files = job.get("exported_files") or {}

            raw_segments: List[Dict[str, Any]] = []

            # 1. Try segments.json
            seg_file: Optional[Path] = None
            if exported_files.get("segments") and Path(exported_files["segments"]).is_file():
                seg_file = Path(exported_files["segments"])
            else:
                c_id = job.get("course_id", "")
                s_id = job.get("sub_id", "")
                prefix = f"{c_id}_{s_id}" if c_id and s_id else ""
                if prefix:
                    matches = sorted(out_dir.glob(f"{prefix}*.segments.json"))
                    if matches:
                        seg_file = matches[0]

            if seg_file and seg_file.is_file():
                try:
                    loaded = json.loads(seg_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        raw_segments = loaded
                except Exception:
                    pass

            # 2. Try checkpoint.json if not found
            if not raw_segments:
                cp_file = out_dir / "checkpoint.json"
                if cp_file.is_file():
                    try:
                        cp_data = json.loads(cp_file.read_text(encoding="utf-8"))
                        if isinstance(cp_data.get("segments"), list):
                            raw_segments = cp_data["segments"]
                    except Exception:
                        pass

            # 3. Read raw.json or checkpoint for confidence metrics (avg_logprob, no_speech_prob)
            raw_metrics_map = {}
            raw_file: Optional[Path] = None
            if exported_files.get("raw") and Path(exported_files["raw"]).is_file():
                raw_file = Path(exported_files["raw"])
            else:
                c_id = job.get("course_id", "")
                s_id = job.get("sub_id", "")
                prefix = f"{c_id}_{s_id}" if c_id and s_id else ""
                if prefix:
                    matches = sorted(out_dir.glob(f"{prefix}*.raw.json"))
                    if matches:
                        raw_file = matches[0]

            if raw_file and raw_file.is_file():
                try:
                    raw_data = json.loads(raw_file.read_text(encoding="utf-8"))
                    idx = 0
                    for chunk in raw_data.get("raw_chunks", []):
                        for s in chunk.get("segments", []):
                            offset = float(chunk.get("start_sec", 0)) + float(job.get("time_offset", 0))
                            key = (round(offset + float(s.get("start", 0)), 2), str(s.get("text", "")).strip())
                            raw_metrics_map[key] = s
                            idx += 1
                except Exception:
                    pass

            # Build preview segments
            result_segments = []
            for idx, seg in enumerate(raw_segments[:max_lines]):
                metric = raw_metrics_map.get((round(float(seg.get("start", 0)), 2), str(seg.get("text", "")).strip()), {})
                avg_lp = seg.get("avg_logprob", metric.get("avg_logprob"))
                nsp = seg.get("no_speech_prob", metric.get("no_speech_prob"))

                needs_review = bool(seg.get("needs_review", False))
                if not needs_review:
                    if avg_lp is not None and float(avg_lp) < -1.0:
                        needs_review = True
                    elif nsp is not None and float(nsp) > 0.6:
                        needs_review = True

                result_segments.append({
                    "start": round(float(seg.get("start", 0.0)), 2),
                    "end": round(float(seg.get("end", 0.0)), 2),
                    "text": str(seg.get("text", "")).strip(),
                    "needs_review": needs_review,
                })

            self._send_json(HTTPStatus.OK, {
                "status": "ok",
                "segments": result_segments,
                "total_segments": len(raw_segments),
                "source_url": source_url,
            })
            return

        if path == "/shutdown":
            self._send_json(HTTPStatus.OK, {"status": "shutting_down"})
            threading.Thread(target=self.server.shutdown).start()
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Endpoint not found: {path}"})


class WorkerServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(
        self,
        server_address: tuple[str, int],
        db: Database,
        token: str,
        asr_engine: Optional[Any] = None,
    ):
        super().__init__(server_address, WorkerRequestHandler)
        self.db = db
        self.token = token
        self.runner = JobRunner(db, asr_engine=asr_engine)
        self._runner_thread = threading.Thread(target=self.runner.run_loop, daemon=True)

    def start_worker(self) -> None:
        self._runner_thread.start()

    def stop_worker(self) -> None:
        self.runner.stop()
        if self._runner_thread.is_alive():
            self._runner_thread.join(timeout=2.0)
        info = load_worker_info()
        if info and info.get('pid') == os.getpid() and info.get('port') == self.server_address[1]:
            remove_worker_info()


def run_server(
    port: int = 48721,
    db_path: Optional[str] = None,
    token: Optional[str] = None,
    asr_engine: Optional[Any] = None,
) -> None:
    worker_log_file = get_app_dir() / "worker.log"
    log_handler = RotatingFileHandler(
        str(worker_log_file),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    log_formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] (worker) %(message)s")
    log_handler.setFormatter(log_formatter)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(log_handler)
    bind_host = "127.0.0.1"  # STRICTLY 127.0.0.1
    auth_token = token or generate_auth_token()
    db = Database(db_path)

    # Bind socket FIRST before DB recovery or thread start
    try:
        server = WorkerServer((bind_host, port), db, token=auth_token, asr_engine=asr_engine)
    except OSError as e:
        logger.error("Failed to bind worker to %s:%d: %s. Another worker instance may be active.", bind_host, port, e)
        sys.exit(1)

    actual_port = server.server_address[1]
    save_worker_info(port=actual_port, pid=os.getpid(), token=auth_token)

    logger.info("Worker bound and started on http://%s:%d (PID %d)", bind_host, actual_port, os.getpid())
    server.start_worker()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user")
    finally:
        server.stop_worker()
        server.server_close()
        logger.info("Worker terminated cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tongji Clipper Background Worker Daemon")
    parser.add_argument("--port", type=int, default=48721, help="Port to bind (127.0.0.1 only)")
    parser.add_argument("--token", type=str, default=None, help="Auth token")
    args = parser.parse_args()
    run_server(port=args.port, token=args.token)


if __name__ == "__main__":
    main()
