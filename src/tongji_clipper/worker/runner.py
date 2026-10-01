"""Sequential job runner executing audio acquisition, ASR chunking, export, and cleanup."""

from __future__ import annotations

import logging
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional
import uuid

from tongji_clipper.acquisition import (
    AcquisitionError,
    SignatureExpiredError,
    extract_pcm_wav,
    probe_audio,
)
from tongji_clipper.asr import ASREngine
from tongji_clipper.config import assert_safe_id, get_default_output_dir, sanitize_url
from tongji_clipper.db import Database
from tongji_clipper.exporter import cleanup_temporary_audio, export_job_results, write_atomic
from tongji_clipper.models import JobConfig, JobStatus

logger = logging.getLogger(__name__)


def get_cpu_threads_for_profile(profile: str) -> int:
    cpus = os.cpu_count() or 4
    low_bound = max(1, min(2, cpus // 4))
    bal_bound = max(low_bound, min(4, max(1, cpus // 2)))
    fast_bound = max(bal_bound, min(8, max(1, cpus - 1)))
    if profile == "low":
        return low_bound
    elif profile == "fast":
        return fast_bound
    else:  # balanced
        return bal_bound


def apply_process_priority(profile: str) -> None:
    if profile == "low":
        try:
            if os.name == "nt":
                import ctypes
                # BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
                ctypes.windll.kernel32.SetPriorityClass(
                    ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000
                )
            else:
                os.nice(10)
        except Exception:
            pass
    elif profile in ("balanced", "fast"):
        try:
            if os.name == "nt":
                import ctypes
                # NORMAL_PRIORITY_CLASS = 0x00000020
                ctypes.windll.kernel32.SetPriorityClass(
                    ctypes.windll.kernel32.GetCurrentProcess(), 0x00000020
                )
            else:
                pass
        except Exception:
            pass


class JobRunner:
    def __init__(self, db: Database, asr_engine: Optional[ASREngine] = None):
        self.db = db
        self.asr_engine = asr_engine
        self._stop_event = threading.Event()
        self._current_job_id: Optional[str] = None
        self._current_run_id: Optional[str] = None
        self._cancelled_runs: set[str] = set()
        self._paused_runs: set[str] = set()
        self._active_ffmpeg_proc: List[subprocess.Popen] = []
        self._prefetch_proc: List[subprocess.Popen] = []
        self._prefetch_job_id: Optional[str] = None
        self._prefetch_run_id: Optional[str] = None
        self._prefetch_thread: Optional[threading.Thread] = None
        self._prefetch_cancel_event = threading.Event()
        self._lock = threading.Lock()
        self.last_heartbeat: float = time.time()
        self._is_running: bool = False

    @property
    def runner_alive(self) -> bool:
        return self._is_running and not self._stop_event.is_set()

    def _record_interrupted(self, job_id: str, run_id: Optional[str] = None) -> None:
        # Check cancellation FIRST (cancellation strictly takes precedence over pause)
        is_cancelling_or_cancelled = False
        if run_id and run_id in self._cancelled_runs:
            is_cancelling_or_cancelled = True
        else:
            row = self.db.get_job(job_id)
            if row and row['status'] in (JobStatus.CANCELLING.value, JobStatus.CANCELLED.value):
                is_cancelling_or_cancelled = True

        if is_cancelling_or_cancelled:
            self.db.update_job_status(
                job_id,
                JobStatus.CANCELLED,
                run_id=run_id,
                error_message='Cancelled by user',
                progress_info={"stage": "cancelled", "message": "任务已取消"},
            )
        else:
            is_pausing_or_paused = False
            if run_id and run_id in self._paused_runs:
                is_pausing_or_paused = True
            else:
                row = self.db.get_job(job_id)
                if row and row['status'] in (JobStatus.PAUSING.value, JobStatus.PAUSED.value):
                    is_pausing_or_paused = True

            if is_pausing_or_paused:
                self.db.update_job_status(
                    job_id,
                    JobStatus.PAUSED,
                    run_id=run_id,
                    error_message="Paused by user",
                    progress_info={"stage": "paused", "message": "任务已暂停"},
                )
            elif self._stop_event.is_set():
                self.db.update_job_status(
                    job_id,
                    JobStatus.QUEUED,
                    run_id=run_id,
                    error_message='Worker stopped; task will resume',
                    progress_info={"stage": "queued", "message": "服务停止，等待恢复"},
                )
            else:
                self.db.update_job_status(
                    job_id,
                    JobStatus.CANCELLED,
                    run_id=run_id,
                    error_message='Cancelled by user',
                    progress_info={"stage": "cancelled", "message": "任务已取消"},
                )

    def _record_cancelled(self, job_id: str, run_id: Optional[str] = None) -> None:
        self._record_interrupted(job_id, run_id)

    def stop(self) -> None:
        """Normal worker stop: signals stop event and terminates active processes without marking cancelled."""
        self._stop_event.set()
        self._prefetch_cancel_event.set()
        with self._lock:
            for proc in list(self._active_ffmpeg_proc):
                try:
                    proc.terminate()
                except OSError:
                    pass
            for proc in list(self._prefetch_proc):
                try:
                    proc.terminate()
                except OSError:
                    pass
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            if self._current_job_id == job_id and self._current_run_id:
                self._cancelled_runs.add(self._current_run_id)
                self.db.cancel_job(job_id, to_cancelling=True)
                for proc in list(self._active_ffmpeg_proc):
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                return True
            if self._prefetch_job_id == job_id:
                self._prefetch_cancel_event.set()
                for proc in list(self._prefetch_proc):
                    try:
                        proc.terminate()
                    except OSError:
                        pass
            row = self.db.get_job(job_id)
            if row and row['status'] == JobStatus.RUNNING.value:
                return self.db.cancel_job(job_id, to_cancelling=True)
            return self.db.cancel_job(job_id, to_cancelling=False)

    def pause_job(self, job_id: str) -> bool:
        with self._lock:
            if self._current_job_id == job_id and self._current_run_id:
                self._paused_runs.add(self._current_run_id)
                self.db.pause_job(job_id)
                for proc in list(self._active_ffmpeg_proc):
                    try:
                        proc.terminate()
                    except OSError:
                        pass
                return True
            if self._prefetch_job_id == job_id:
                self._prefetch_cancel_event.set()
                for proc in list(self._prefetch_proc):
                    try:
                        proc.terminate()
                    except OSError:
                        pass
            return self.db.pause_job(job_id)

    def resume_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.db.resume_job(job_id)

    def retry_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.db.retry_job(job_id)

    def cancel_current(self) -> None:
        with self._lock:
            if self._current_run_id:
                self._cancelled_runs.add(self._current_run_id)
                if self._current_job_id:
                    self.db.cancel_job(self._current_job_id, to_cancelling=True)
                for proc in list(self._active_ffmpeg_proc):
                    try:
                        proc.terminate()
                    except OSError:
                        pass

    def _maybe_start_prefetch(self, base_out: Optional[Path] = None) -> None:
        """Starts audio prefetch for at most one subsequent queued job during ASR phase."""
        with self._lock:
            if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
                return
            if self._stop_event.is_set():
                return

        next_job = self.db.peek_next_queued_job(exclude_job_id=self._current_job_id)
        if not next_job:
            return

        next_job_id = next_job["job_id"]
        next_cfg = JobConfig.from_dict(next_job.get("config", {}))
        if base_out is not None:
            next_base_out = Path(base_out)
        else:
            next_base_out = Path(next_cfg.output_dir) if next_cfg.output_dir else get_default_output_dir()
        safe_course = assert_safe_id(next_job.get("course_id", "course"), "course_id")
        safe_sub = assert_safe_id(next_job.get("sub_id", "sub"), "sub_id")
        job_dir_name = f"{safe_course}_{safe_sub}_{next_job_id}"
        next_job_dir = next_base_out / job_dir_name

        wav_path = next_job_dir / "audio_16k_mono.wav"
        marker_path = next_job_dir / "acquisition.json"
        if wav_path.is_file() and marker_path.is_file():
            # Already acquired and verified
            return

        try:
            total_cache = self.db.get_total_cache_bytes()
            if total_cache >= 512 * 1024 * 1024:
                logger.info("Cache budget reached (>=512MB); skipping audio prefetch")
                return

            next_base_out.mkdir(parents=True, exist_ok=True)
            free_space = shutil.disk_usage(str(next_base_out)).free
            if free_space < 1024 * 1024 * 1024:
                logger.warning("Low disk space (<1GB); skipping audio prefetch")
                return
        except Exception as e:
            logger.warning("Could not check disk/cache limits: %s", e)
            return

        prefetch_run_id = f"prefetch_{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._prefetch_job_id = next_job_id
            self._prefetch_run_id = prefetch_run_id
            self._prefetch_cancel_event.clear()

        def do_prefetch():
            try:
                self._run_prefetch(next_job, next_job_dir, prefetch_run_id)
            except Exception:
                logger.exception("Audio prefetch failed for %s", next_job_id)
            finally:
                with self._lock:
                    if self._prefetch_run_id == prefetch_run_id:
                        self._prefetch_job_id = None
                        self._prefetch_run_id = None
                        self._prefetch_proc.clear()

        self._prefetch_thread = threading.Thread(target=do_prefetch, daemon=True, name=f"prefetch_{next_job_id}")
        self._prefetch_thread.start()

    def _run_prefetch(
        self,
        next_job: Dict[str, Any],
        target_dir: Optional[Path] = None,
        prefetch_run_id: Optional[str] = None,
    ) -> None:
        next_job_id = next_job["job_id"]
        safe_course = assert_safe_id(next_job.get("course_id", "course"), "course_id")
        safe_sub = assert_safe_id(next_job.get("sub_id", "sub"), "sub_id")
        job_dir_name = f"{safe_course}_{safe_sub}_{next_job_id}"

        if target_dir is None:
            next_cfg = JobConfig.from_dict(next_job.get("config", {}))
            base_out = Path(next_cfg.output_dir) if next_cfg.output_dir else get_default_output_dir()
            next_job_dir = base_out / job_dir_name
        else:
            p = Path(target_dir)
            if p.name == job_dir_name:
                next_job_dir = p
            else:
                next_job_dir = p / job_dir_name

        if prefetch_run_id is None:
            prefetch_run_id = f"prefetch_{uuid.uuid4().hex[:8]}"

        next_job_dir.mkdir(parents=True, exist_ok=True)
        # Register next_job's output_dir in DB
        if not self.db.reserve_prefetch(next_job_id, prefetch_run_id, str(next_job_dir)):
            return

        final_wav = next_job_dir / "audio_16k_mono.wav"
        marker_file = next_job_dir / "acquisition.json"
        part_wav = next_job_dir / f"audio_16k_mono.wav.prefetch_{prefetch_run_id}.part"

        def check_prefetch_cancelled() -> bool:
            if self._stop_event.is_set() or self._prefetch_cancel_event.is_set():
                return True
            curr = self.db.get_job(next_job_id)
            if not curr or curr["status"] != JobStatus.QUEUED.value or curr.get("run_id") != prefetch_run_id:
                return True
            return False

        def on_prefetch_progress(p_info: Dict[str, Any]) -> None:
            if check_prefetch_cancelled():
                return
            # Continuous budget & disk space check
            part_size = part_wav.stat().st_size if part_wav.exists() else 0
            if (self.db.get_total_cache_bytes() + part_size) >= 512 * 1024 * 1024:
                logger.warning("Cache budget exceeded (>=512MB) during prefetch for job %s; aborting", next_job_id)
                self._prefetch_cancel_event.set()
                for p in list(self._prefetch_proc):
                    try:
                        p.terminate()
                    except OSError:
                        pass
                return

            if shutil.disk_usage(str(next_job_dir)).free < 1024 * 1024 * 1024:
                logger.warning("Disk space low (<1GB) during prefetch for job %s; aborting", next_job_id)
                self._prefetch_cancel_event.set()
                for p in list(self._prefetch_proc):
                    try:
                        p.terminate()
                    except OSError:
                        pass
                return

        signed_url = next_job.get("signed_url", "")
        max_retries = 2
        try:
            for attempt in range(max_retries + 1):
                if check_prefetch_cancelled():
                    return
                try:
                    logger.info("Prefetching audio for next job %s (attempt %d)...", next_job_id, attempt + 1)
                    audio_meta = extract_pcm_wav(
                        source_url_or_path=signed_url,
                        output_wav_path=str(part_wav),
                        check_cancelled=check_prefetch_cancelled,
                        process_holder=self._prefetch_proc,
                        on_progress=on_prefetch_progress,
                    )
                    if check_prefetch_cancelled():
                        return
                    # Atomically rename .part to final wav, then write acquisition.json
                    if not self.db.publish_prefetch(next_job_id, prefetch_run_id, part_wav, final_wav, marker_file, audio_meta):
                        return
                    logger.info("Prefetched audio successfully for job %s", next_job_id)
                    return
                except SignatureExpiredError as e:
                    logger.warning("Prefetch for job %s encountered expired signature: %s", next_job_id, e)
                    self.db.update_job_status(
                        next_job_id,
                        JobStatus.NEEDS_REFRESH,
                        run_id=prefetch_run_id,
                        error_message=f"媒体签名已失效或无权访问（401/403），请刷新页面后重新提交签名: {e}",
                    )
                    return
                except AcquisitionError as e:
                    if check_prefetch_cancelled():
                        return
                    logger.warning("Prefetch attempt %d for job %s failed: %s", attempt + 1, next_job_id, e)
                    if attempt < max_retries:
                        time.sleep(1.0 * (attempt + 1))
                    else:
                        logger.error("Prefetch for job %s failed after %d attempts", next_job_id, max_retries + 1)
                        return
        finally:
            if part_wav.is_file():
                try:
                    part_wav.unlink()
                except OSError:
                    pass

    def run_loop(self) -> None:
        """Main worker loop processing jobs sequentially."""
        logger.info("Starting sequential job runner loop...")
        self._stop_event.clear()
        self._is_running = True
        heartbeat_done = threading.Event()
        def heartbeat():
            while not heartbeat_done.wait(1.0):
                self.last_heartbeat = time.time()
        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True, name="worker-heartbeat")
        heartbeat_thread.start()
        try:
            recovered = self.db.recover_interrupted_jobs()
            if recovered > 0:
                logger.info("Recovered %d interrupted jobs back to queued state", recovered)

            while not self._stop_event.is_set():
                self.last_heartbeat = time.time()
                if self.asr_engine:
                    self.asr_engine.check_idle_unload()

                # If the next queued job is being prefetched, wait for prefetch to finish before claiming
                with self._lock:
                    p_thread = self._prefetch_thread
                    p_job_id = self._prefetch_job_id

                # Never start a second acquisition while any prefetch still owns a file.
                if p_thread and p_thread.is_alive():
                    p_thread.join(timeout=0.5)
                    continue
                if self._stop_event.is_set():
                    break

                job = self.db.get_next_queued_job()
                if not job:
                    time.sleep(1.0)
                    continue

                self._process_single_job(job)
        finally:
            heartbeat_done.set()
            heartbeat_thread.join(timeout=2.0)
            self._is_running = False

    def _process_single_job(self, job: Dict[str, Any]) -> None:
        job_id = job["job_id"]
        run_id = job.get("run_id")

        with self._lock:
            self._current_job_id = job_id
            self._current_run_id = run_id
            self._active_ffmpeg_proc.clear()

            # Handle claim-to-lock race: check DB status under lock
            curr_db_job = self.db.get_job(job_id)
            if not curr_db_job:
                return
            db_status = curr_db_job["status"]
            if db_status == JobStatus.CANCELLING.value:
                if run_id:
                    self._cancelled_runs.add(run_id)
            elif db_status == JobStatus.PAUSING.value:
                if run_id:
                    self._paused_runs.add(run_id)
            elif db_status in (JobStatus.CANCELLED.value, JobStatus.PAUSED.value):
                return

        def is_cancelled() -> bool:
            return self._stop_event.is_set() or (bool(run_id) and run_id in self._cancelled_runs)

        def is_paused() -> bool:
            return bool(run_id) and run_id in self._paused_runs

        def is_interrupted() -> bool:
            return is_cancelled() or is_paused()

        if is_interrupted():
            self._record_interrupted(job_id, run_id)
            return

        start_time = time.time()
        logger.info("Starting processing job: %s (%s)", job_id, job.get("course_title"))

        self.db.update_job_status(
            job_id,
            JobStatus.RUNNING,
            run_id=run_id,
            progress_info={"stage": "preparing", "message": "准备任务"},
        )

        cfg_dict = job.get("config", {})
        config = JobConfig.from_dict(cfg_dict)

        # Output folder with full job_id and safe IDs
        try:
            base_out = Path(config.output_dir) if config.output_dir else get_default_output_dir()
            safe_course = assert_safe_id(job.get("course_id", "course"), "course_id")
            safe_sub = assert_safe_id(job.get("sub_id", "sub"), "sub_id")
            job_dir_name = f"{safe_course}_{safe_sub}_{job_id}"
            job_output_dir = base_out / job_dir_name
            job_output_dir.mkdir(parents=True, exist_ok=True)
            self.db.update_job_status(
                job_id,
                JobStatus.RUNNING,
                run_id=run_id,
                output_dir=str(job_output_dir),
            )
        except Exception as e:
            logger.error("Failed to prepare output directory for job %s: %s", job_id, e)
            self.db.update_job_status(
                job_id,
                JobStatus.FAILED,
                run_id=run_id,
                error_message=f"无法创建输出目录: {e}",
            )
            return

        temp_wav_path = str(job_output_dir / "audio_16k_mono.wav")
        checkpoint_path = str(job_output_dir / "checkpoint.json")
        acquired_marker = job_output_dir / "acquisition.json"

        timings: Dict[str, float] = {
            "acquisition": 0.0,
            "asr": 0.0,
            "export": 0.0,
            "total": 0.0,
        }

        try:
            # Stage 1: Audio acquisition
            if is_interrupted():
                self._record_interrupted(job_id, run_id)
                return

            self.db.update_job_status(
                job_id,
                JobStatus.RUNNING,
                run_id=run_id,
                progress_info={"stage": "acquisition", "message": "正在提取/转换 16kHz WAV 音频..."},
            )

            signed_url = job.get("signed_url", "")
            time_offset = float(job.get("time_offset", 0.0))

            def on_acq_progress(p_info: Dict[str, Any]) -> None:
                if is_interrupted():
                    return
                self.db.update_job_status(
                    job_id,
                    JobStatus.RUNNING,
                    run_id=run_id,
                    progress_info=p_info,
                )

            t_acq_start = time.time()
            try:
                reuse_verified_audio = False
                if acquired_marker.is_file() and Path(temp_wav_path).is_file():
                    try:
                        audio_meta = probe_audio(temp_wav_path)
                        recorded = json.loads(acquired_marker.read_text(encoding="utf-8"))
                        reuse_verified_audio = recorded == audio_meta
                    except (AcquisitionError, ValueError, OSError):
                        pass
                if not reuse_verified_audio:
                    acquired_marker.unlink(missing_ok=True)
                    audio_meta = extract_pcm_wav(
                        source_url_or_path=signed_url,
                        output_wav_path=temp_wav_path,
                        check_cancelled=is_interrupted,
                        process_holder=self._active_ffmpeg_proc,
                        on_progress=on_acq_progress,
                    )
                    write_atomic(acquired_marker, json.dumps(audio_meta))
            except SignatureExpiredError as e:
                logger.warning("Job %s signature expired: %s", job_id, e)
                self.db.update_job_status(
                    job_id,
                    JobStatus.NEEDS_REFRESH,
                    run_id=run_id,
                    error_message=f"媒体签名已失效或无权访问（401/403），请刷新页面后重新提交签名: {e}",
                )
                return
            except AcquisitionError as e:
                if is_interrupted():
                    self._record_interrupted(job_id, run_id)
                    return
                logger.error("Job %s acquisition failed: %s", job_id, e)
                self.db.update_job_status(
                    job_id,
                    JobStatus.FAILED,
                    run_id=run_id,
                    error_message=str(e),
                )
                return

            timings["acquisition"] = time.time() - t_acq_start

            if is_interrupted():
                self._record_interrupted(job_id, run_id)
                return

            # Stage 2: ASR
            total_duration = audio_meta["duration"]
            logger.info("Audio acquired in %.2fs, duration: %.2fs. Beginning ASR...", timings["acquisition"], total_duration)

            # Apply resource profile (process priority & cpu_threads)
            apply_process_priority(config.resource_profile)
            cpu_threads = get_cpu_threads_for_profile(config.resource_profile)

            # Ensure matching engine instance is used
            from tongji_clipper.config import get_models_cache_dir
            model_root = config.model_dir or str(get_models_cache_dir())
            needed_sig = f"{config.model}:{config.device}:{config.compute_type}:{model_root}:{config.offline}:{cpu_threads}"
            is_mock = getattr(self.asr_engine, "_mock_transcribe_fn", None) is not None

            if not is_mock:
                curr_sig = getattr(self.asr_engine, "engine_signature", None)
                if self.asr_engine is None or (curr_sig and curr_sig != needed_sig):
                    logger.info("Initializing ASR engine for signature: %s", needed_sig)
                    self.asr_engine = ASREngine(
                        model_name=config.model,
                        device=config.device,
                        compute_type=config.compute_type,
                        download_root=config.model_dir,
                        offline=config.offline,
                        cpu_threads=cpu_threads,
                    )

            # Stage 2a: Model loading notice
            self.db.update_job_status(
                job_id,
                JobStatus.RUNNING,
                run_id=run_id,
                progress_info={"stage": "loading", "message": "正在加载模型与准备转写，首次加载可能较慢…"},
            )

            # Stage 2b: Trigger prefetch for next queued job ahead of time while network is idle
            self._maybe_start_prefetch()

            self.asr_engine.load_model()
            if is_interrupted():
                self._record_interrupted(job_id, run_id)
                return
            t_asr_start = time.time()

            initial_checkpoint_chunks = 0
            if Path(checkpoint_path).is_file():
                try:
                    cp_data = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
                    initial_checkpoint_chunks = len(cp_data.get("raw_chunks", []))
                except Exception:
                    pass

            def on_asr_progress(curr_chunk: int, total_chunks: int, msg: str) -> None:
                if is_interrupted():
                    return
                now = time.time()
                self.last_heartbeat = now
                elapsed = round(now - t_asr_start, 1)
                pct = round((curr_chunk / total_chunks) * 100.0, 1) if total_chunks > 0 else 0.0
                step = config.chunk_duration - config.chunk_overlap
                processed_sec = min(total_duration, curr_chunk * step)
                if curr_chunk >= total_chunks:
                    processed_sec = total_duration

                eta_sec = None
                new_chunks = max(0, curr_chunk - initial_checkpoint_chunks)
                new_processed_sec = new_chunks * step
                if new_chunks > 0 and elapsed > 0 and new_processed_sec > 0:
                    speed_ratio = new_processed_sec / elapsed
                    if speed_ratio > 0:
                        rem_audio = max(0.0, total_duration - processed_sec)
                        eta_sec = round(rem_audio / speed_ratio, 1)
                if curr_chunk >= total_chunks:
                    eta_sec = 0.0

                self.db.update_job_status(
                    job_id,
                    JobStatus.RUNNING,
                    run_id=run_id,
                    progress_info={
                        "stage": "transcription",
                        "current_chunk": min(curr_chunk + 1, total_chunks),
                        "completed_chunks": curr_chunk,
                        "total_chunks": total_chunks,
                        "chunk_percent": pct,
                        "processed_seconds": round(processed_sec, 1),
                        "total_seconds": round(total_duration, 1),
                        "elapsed_seconds": round(now - start_time, 1),
                        "eta_seconds": eta_sec,
                        "message": msg,
                    },
                )

            try:
                segments = self.asr_engine.transcribe_audio_stream(
                    audio_path=temp_wav_path,
                    total_duration=total_duration,
                    chunk_duration=config.chunk_duration,
                    chunk_overlap=config.chunk_overlap,
                    base_time_offset=time_offset,
                    language=config.language,
                    checkpoint_path=checkpoint_path,
                    on_progress=on_asr_progress,
                    check_cancelled=is_interrupted,
                )
            except Exception as e:
                if is_interrupted():
                    self._record_interrupted(job_id, run_id)
                    return
                logger.error("Job %s ASR failed: %s", job_id, e)
                self.db.update_job_status(
                    job_id,
                    JobStatus.FAILED,
                    run_id=run_id,
                    error_message=f"ASR 转写错误: {e}",
                )
                return

            timings["asr"] = time.time() - t_asr_start

            if is_interrupted():
                self._record_interrupted(job_id, run_id)
                return

            # Stage 3: Export results
            self.db.update_job_status(
                job_id,
                JobStatus.RUNNING,
                run_id=run_id,
                progress_info={"stage": "exporting", "message": "正在写入并核验逐字稿..."},
            )

            timings["total"] = time.time() - start_time
            exported_files = export_job_results(
                job_info=job,
                segments=segments,
                output_dir=job_output_dir,
                timings=timings,
            )

            # Persist manifest
            try:
                write_atomic(
                    job_output_dir / "manifest.json",
                    json.dumps({"job_id": job_id, "exported_files": exported_files}, indent=2, ensure_ascii=False),
                )
            except Exception:
                pass

            # Cleanup temporary wav only after verifying output files
            if not config.keep_audio:
                cleanup_temporary_audio(temp_wav_path, verification_paths=exported_files)

            # Guard against cancellation/pause during export/cleanup
            if is_interrupted():
                self._record_interrupted(job_id, run_id)
                return

            curr = self.db.get_job(job_id)
            if curr and curr["status"] in (
                JobStatus.CANCELLING.value,
                JobStatus.CANCELLED.value,
                JobStatus.PAUSING.value,
                JobStatus.PAUSED.value,
            ):
                self._record_interrupted(job_id, run_id)
                return

            self.db.update_job_status(
                job_id,
                JobStatus.COMPLETED,
                run_id=run_id,
                output_dir=str(job_output_dir),
                exported_files=exported_files,
                progress_info={"stage": "completed", "message": "转写完成", "processed_seconds": total_duration,
                               "total_seconds": total_duration, "elapsed_seconds": round(time.time() - start_time, 1)},
            )
            logger.info("Job %s completed successfully in %.2fs", job_id, timings["total"])

        except Exception as e:
            logger.error("Unexpected error processing job %s: %s", job_id, e)
            self.db.update_job_status(job_id, JobStatus.FAILED, run_id=run_id, error_message=f"未知错误: {e}")
        finally:
            with self._lock:
                if self._current_run_id == run_id:
                    self._current_job_id = None
                    self._current_run_id = None
                    self._active_ffmpeg_proc.clear()
                if run_id:
                    self._cancelled_runs.discard(run_id)
                    self._paused_runs.discard(run_id)
