"""Sequential job runner executing audio acquisition, ASR chunking, export, and cleanup."""

from __future__ import annotations

import logging
import json
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from tongji_clipper.acquisition import (
    AcquisitionError,
    SignatureExpiredError,
    extract_pcm_wav,
    probe_audio,
)
from tongji_clipper.asr import ASREngine
from tongji_clipper.config import assert_safe_id, get_default_output_dir, sanitize_url
from tongji_clipper.db import Database
from tongji_clipper.exporter import cleanup_temporary_audio, export_job_results
from tongji_clipper.models import JobConfig, JobStatus

logger = logging.getLogger(__name__)


class JobRunner:
    def _record_cancelled(self, job_id: str) -> None:
        status = JobStatus.QUEUED if self._stop_event.is_set() else JobStatus.CANCELLED
        self.db.update_job_status(job_id, status, error_message='Worker stopped; task will resume' if self._stop_event.is_set() else 'Cancelled by user')

    def __init__(self, db: Database, asr_engine: Optional[ASREngine] = None):
        self.db = db
        self.asr_engine = asr_engine
        self._stop_event = threading.Event()
        self._current_job_id: Optional[str] = None
        self._cancel_job_id: Optional[str] = None
        self._active_ffmpeg_proc: List[subprocess.Popen] = []
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._stop_event.set()
        self.cancel_current()

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            if self._current_job_id == job_id:
                self._cancel_job_id = job_id
                for proc in list(self._active_ffmpeg_proc):
                    try:
                        proc.terminate()
                    except OSError:
                        pass
            return self.db.cancel_job(job_id)

    def cancel_current(self) -> None:
        with self._lock:
            if self._current_job_id:
                self._cancel_job_id = self._current_job_id
                for proc in list(self._active_ffmpeg_proc):
                    try:
                        proc.terminate()
                    except OSError:
                        pass

    def run_loop(self) -> None:
        """Main worker loop processing jobs sequentially."""
        logger.info("Starting sequential job runner loop...")
        recovered = self.db.recover_interrupted_jobs()
        if recovered > 0:
            logger.info("Recovered %d interrupted jobs back to queued state", recovered)

        while not self._stop_event.is_set():
            job = self.db.get_next_queued_job()
            if not job:
                time.sleep(1.0)
                continue

            self._process_single_job(job)

    def _process_single_job(self, job: Dict[str, Any]) -> None:
        job_id = job["job_id"]
        if self.db.get_job(job_id)['status'] == JobStatus.CANCELLED.value:
            return
        with self._lock:
            self._current_job_id = job_id
            self._cancel_job_id = None
            self._active_ffmpeg_proc.clear()

        start_time = time.time()
        logger.info("Starting processing job: %s (%s)", job_id, job.get("course_title"))

        self.db.update_job_status(
            job_id,
            JobStatus.RUNNING,
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
        except Exception as e:
            logger.error("Failed to prepare output directory for job %s: %s", job_id, e)
            self.db.update_job_status(
                job_id,
                JobStatus.FAILED,
                error_message=f"无法创建输出目录: {e}",
            )
            return

        temp_wav_path = str(job_output_dir / "audio_16k_mono.wav")
        checkpoint_path = str(job_output_dir / "checkpoint.json")
        acquired_marker = job_output_dir / "acquisition.json"

        def is_cancelled() -> bool:
            return self._stop_event.is_set() or (self._cancel_job_id == job_id)

        timings: Dict[str, float] = {
            "acquisition": 0.0,
            "asr": 0.0,
            "export": 0.0,
            "total": 0.0,
        }

        try:
            # Stage 1: Audio acquisition
            if is_cancelled():
                self._record_cancelled(job_id)
                return

            self.db.update_job_status(
                job_id,
                JobStatus.RUNNING,
                progress_info={"stage": "acquisition", "message": "正在提取/转换 16kHz WAV 音频..."},
            )

            signed_url = job.get("signed_url", "")
            time_offset = float(job.get("time_offset", 0.0))

            t_acq_start = time.time()
            try:
                if acquired_marker.is_file() and Path(temp_wav_path).is_file():
                    audio_meta = probe_audio(temp_wav_path)
                    recorded = json.loads(acquired_marker.read_text(encoding='utf-8'))
                    if recorded != audio_meta:
                        raise AcquisitionError("Cached audio differs from verified acquisition")
                else:
                    audio_meta = extract_pcm_wav(
                        source_url_or_path=signed_url,
                        output_wav_path=temp_wav_path,
                        check_cancelled=is_cancelled,
                        process_holder=self._active_ffmpeg_proc,
                    )
                    acquired_marker.write_text(json.dumps(audio_meta), encoding='utf-8')
            except SignatureExpiredError as e:
                logger.warning("Job %s signature expired: %s", job_id, e)
                self.db.update_job_status(
                    job_id,
                    JobStatus.NEEDS_REFRESH,
                    error_message=f"媒体签名已失效或无权访问（401/403），请刷新页面后重新提交签名: {e}",
                )
                return
            except AcquisitionError as e:
                if is_cancelled():
                    self._record_cancelled(job_id)
                    return
                logger.error("Job %s acquisition failed: %s", job_id, e)
                self.db.update_job_status(
                    job_id,
                    JobStatus.FAILED,
                    error_message=str(e),
                )
                return

            timings["acquisition"] = time.time() - t_acq_start

            if is_cancelled():
                self._record_cancelled(job_id)
                return

            # Stage 2: ASR
            total_duration = audio_meta["duration"]
            logger.info("Audio acquired in %.2fs, duration: %.2fs. Beginning ASR...", timings["acquisition"], total_duration)

            # Ensure matching engine instance is used (reload if config differs, preserve mock if set)
            from tongji_clipper.config import get_models_cache_dir
            model_root = config.model_dir or str(get_models_cache_dir())
            needed_sig = f"{config.model}:{config.device}:{config.compute_type}:{model_root}:{config.offline}"
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
                    )

            def on_asr_progress(curr_chunk: int, total_chunks: int, msg: str) -> None:
                pct = round((curr_chunk / total_chunks) * 100.0, 1) if total_chunks > 0 else 0.0
                self.db.update_job_status(
                    job_id,
                    JobStatus.RUNNING,
                    progress_info={
                        "stage": "transcription",
                        "current_chunk": min(curr_chunk + 1, total_chunks),
                        "completed_chunks": curr_chunk,
                        "total_chunks": total_chunks,
                        "chunk_percent": pct,
                        "message": msg,
                    },
                )

            t_asr_start = time.time()
            on_asr_progress(0, 0, "正在加载模型与准备转写，首次加载可能较慢…")
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
                    check_cancelled=is_cancelled,
                )
            except Exception as e:
                if is_cancelled():
                    self._record_cancelled(job_id)
                    return
                logger.error("Job %s ASR failed: %s", job_id, e)
                self.db.update_job_status(
                    job_id,
                    JobStatus.FAILED,
                    error_message=f"ASR 转写错误: {e}",
                )
                return

            timings["asr"] = time.time() - t_asr_start

            if is_cancelled():
                self._record_cancelled(job_id)
                return

            # Stage 3: Export results
            self.db.update_job_status(
                job_id,
                JobStatus.RUNNING,
                progress_info={"stage": "exporting", "message": "正在写入并核验逐字稿..."},
            )

            t_exp_start = time.time()
            timings["total"] = time.time() - start_time
            timings["export"] = time.time() - t_exp_start

            exported_files = export_job_results(
                job_info=job,
                segments=segments,
                output_dir=job_output_dir,
                timings=timings,
            )

            # Cleanup temporary wav only after verifying output files
            if not config.keep_audio:
                cleanup_temporary_audio(temp_wav_path, verification_paths=exported_files)

            self.db.update_job_status(
                job_id,
                JobStatus.COMPLETED,
                output_dir=str(job_output_dir),
                progress_info={"stage": "completed", "message": "转写完成"},
            )
            logger.info("Job %s completed successfully in %.2fs", job_id, timings["total"])

        except Exception as e:
            logger.error("Unexpected error processing job %s: %s", job_id, e)
            self.db.update_job_status(job_id, JobStatus.FAILED, error_message=f"未知错误: {e}")
        finally:
            with self._lock:
                self._current_job_id = None
                self._cancel_job_id = None
                self._active_ffmpeg_proc.clear()
