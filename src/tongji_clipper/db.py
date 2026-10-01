"""SQLite persistent queue, idempotency, signature isolation, and crash recovery."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import sqlite3
from typing import Any, Dict, List, Optional
import uuid

from tongji_clipper.config import get_db_path
from tongji_clipper.models import (
    CapturePayload,
    JobConfig,
    JobStatus,
    assert_safe_id,
    compute_idempotency_key,
    compute_job_id,
    validate_signed_url,
)


class Database:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or str(get_db_path())
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    tenant_code TEXT NOT NULL,
                    course_id TEXT NOT NULL,
                    sub_id TEXT NOT NULL,
                    stream_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    course_title TEXT,
                    sub_title TEXT,
                    lecturer_name TEXT,
                    room_name TEXT,
                    signed_url TEXT NOT NULL,
                    time_offset REAL NOT NULL DEFAULT 0.0,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    output_dir TEXT,
                    config_json TEXT NOT NULL,
                    progress_info TEXT,
                    run_id TEXT,
                    exported_files_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            try:
                conn.execute("ALTER TABLE jobs ADD COLUMN run_id TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE jobs ADD COLUMN exported_files_json TEXT")
            except sqlite3.OperationalError:
                pass
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_idempotency ON jobs(idempotency_key)"
            )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def submit_job(
        self,
        capture: CapturePayload,
        stream_id: str,
        config: JobConfig,
    ) -> Dict[str, Any]:
        """
        Idempotent submission.
        - If completed: returns existing job without re-running.
        - If running: returns existing job.
        - If queued: updates signed_url to latest and returns existing job.
        - If needs_refresh / failed / cancelled: updates signed_url and resets to queued.
        """
        capture.validate()
        target_stream = None
        for s in capture.media_streams:
            if s.stream_id == stream_id:
                target_stream = s
                break

        if not target_stream:
            if capture.media_streams:
                target_stream = capture.media_streams[0]
                stream_id = target_stream.stream_id
            else:
                raise ValueError(f"Stream {stream_id} not found in capture payload")

        idempotency_key = compute_idempotency_key(
            tenant_code=capture.tenant_code,
            course_id=capture.course_id,
            sub_id=capture.sub_id,
            stream_id=stream_id,
            time_offset=target_stream.time_offset,
            config=config,
            local_file_path=capture.local_file_path,
        )
        job_id = compute_job_id(idempotency_key)
        now = self._now_iso()
        config_json = json.dumps(config.to_dict())

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()

            if not row:
                from tongji_clipper.models import compute_legacy_idempotency_key
                candidates = conn.execute(
                    "SELECT * FROM jobs WHERE tenant_code = ? AND course_id = ? AND sub_id = ? AND stream_id = ?",
                    (capture.tenant_code, capture.course_id, capture.sub_id, stream_id),
                ).fetchall()
                for candidate in candidates:
                    old_config = JobConfig.from_dict(json.loads(candidate["config_json"]))
                    identity_args = dict(tenant_code=capture.tenant_code, course_id=capture.course_id,
                                         sub_id=capture.sub_id, stream_id=stream_id,
                                         time_offset=target_stream.time_offset, config=old_config,
                                         local_file_path=capture.local_file_path)
                    if (compute_idempotency_key(**identity_args) == idempotency_key
                            and compute_legacy_idempotency_key(**identity_args) == candidate["idempotency_key"]):
                        row = candidate
                        break

            if row:
                current_status = row["status"]
                if current_status == JobStatus.COMPLETED.value:
                    return self._row_to_dict(row, include_signature=False)
                elif current_status == JobStatus.RUNNING.value:
                    return self._row_to_dict(row, include_signature=False)
                elif current_status == JobStatus.QUEUED.value:
                    # Update signed_url of queued job with newer signature
                    conn.execute(
                        "UPDATE jobs SET signed_url = ?, run_id = NULL, updated_at = ? WHERE job_id = ?",
                        (target_stream.signed_url, now, row["job_id"]),
                    )
                    updated = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
                    return self._row_to_dict(updated, include_signature=False)
                elif current_status in (
                    JobStatus.NEEDS_REFRESH.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.CANCELLING.value,
                    JobStatus.PAUSED.value,
                    JobStatus.PAUSING.value,
                ):
                    # Update signed_url and requeue, invalidating any previous run_id
                    conn.execute(
                        """
                        UPDATE jobs
                        SET signed_url = ?,
                            time_offset = ?,
                            status = ?,
                            error_message = NULL,
                            progress_info = NULL,
                            run_id = NULL,
                            updated_at = ?
                        WHERE job_id = ?
                        """,
                        (target_stream.signed_url, target_stream.time_offset, JobStatus.QUEUED.value, now, row["job_id"]),
                    )
                    updated_row = conn.execute(
                        "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
                    ).fetchone()
                    return self._row_to_dict(updated_row, include_signature=False)

            # Insert new job
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, idempotency_key, tenant_code, course_id, sub_id, stream_id,
                    source_url, course_title, sub_title, lecturer_name, room_name,
                    signed_url, time_offset, status, error_message, output_dir,
                    config_json, progress_info, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    capture.tenant_code,
                    capture.course_id,
                    capture.sub_id,
                    stream_id,
                    capture.source_url,
                    capture.course_title,
                    capture.sub_title,
                    capture.lecturer_name,
                    capture.room_name,
                    target_stream.signed_url,
                    target_stream.time_offset,
                    JobStatus.QUEUED.value,
                    None,
                    config.output_dir,
                    config_json,
                    json.dumps({"stage": "queued", "percent": 0.0}),
                    now,
                    now,
                ),
            )
            new_row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return self._row_to_dict(new_row, include_signature=False)

    def refresh_signature(self, job_id: str, new_signed_url: str) -> Optional[Dict[str, Any]]:
        """
        Updates signed_url for a job and moves it back to queued.
        Strictly permitted ONLY if current status is in (needs_refresh, failed, cancelled).
        Rejects running or completed jobs!
        """
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None

            current_status = row["status"]
            if current_status not in (JobStatus.NEEDS_REFRESH.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value):
                raise ValueError(
                    f"Cannot refresh signature for job in '{current_status}' state (only allowed for needs_refresh, failed, cancelled)"
                )

            # Validate the new URL
            if row["tenant_code"] != "local":
                validate_signed_url(new_signed_url, row["stream_id"])

            conn.execute(
                """
                UPDATE jobs
                SET signed_url = ?,
                    status = ?,
                    error_message = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (new_signed_url, JobStatus.QUEUED.value, now, job_id),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_dict(updated, include_signature=False)

    def recover_interrupted_jobs(self) -> int:
        """
        Crash / startup recovery:
        - Jobs left in 'running' are reset to 'queued' so they resume cleanly.
        - Jobs left in 'pausing' are recovered to 'paused' to preserve user pause intent.
        - Jobs left in 'cancelling' are finalized to 'cancelled'.
        """
        now = self._now_iso()
        with self._get_connection() as conn:
            c1 = conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    error_message = 'Interrupted by worker restart; requeued',
                    run_id = NULL,
                    updated_at = ?
                WHERE status = ?
                """,
                (JobStatus.QUEUED.value, now, JobStatus.RUNNING.value),
            )
            c2 = conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    error_message = 'Paused by user (finalized on restart)',
                    run_id = NULL,
                    updated_at = ?
                WHERE status = ?
                """,
                (JobStatus.PAUSED.value, now, JobStatus.PAUSING.value),
            )
            c3 = conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    error_message = 'Cancelled by user (finalized on restart)',
                    run_id = NULL,
                    updated_at = ?
                WHERE status = ?
                """,
                (JobStatus.CANCELLED.value, now, JobStatus.CANCELLING.value),
            )
            return c1.rowcount + c2.rowcount + c3.rowcount

    def peek_next_queued_job(self, exclude_job_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Inspects the next queued job FIFO without altering status or claiming."""
        with self._get_connection() as conn:
            query = "SELECT * FROM jobs WHERE status = ?"
            params: List[Any] = [JobStatus.QUEUED.value]
            if exclude_job_id:
                query += " AND job_id != ?"
                params.append(exclude_job_id)
            query += " ORDER BY created_at ASC LIMIT 1"
            row = conn.execute(query, params).fetchone()
            if not row:
                return None
            return self._row_to_dict(row, include_signature=True)

    def reserve_prefetch(self, job_id: str, run_id: str, output_dir: str) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute(
                "UPDATE jobs SET run_id = ?, output_dir = ? WHERE job_id = ? AND status = 'queued'",
                (run_id, output_dir, job_id),
            )
            return cur.rowcount == 1

    def publish_prefetch(self, job_id, run_id, part, final, marker, metadata) -> bool:
        from tongji_clipper.exporter import write_atomic
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status, run_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row or row["status"] != "queued" or row["run_id"] != run_id:
                return False
            part.replace(final)
            write_atomic(marker, json.dumps(metadata, ensure_ascii=False))
            return True

    def get_next_queued_job(self) -> Optional[Dict[str, Any]]:
        """Picks the oldest queued job (FIFO). Generates a fresh run_id and transitions to RUNNING."""
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (JobStatus.QUEUED.value,),
            ).fetchone()
            if not row:
                return None
            run_id = f"run_{uuid.uuid4().hex[:12]}"
            conn.execute(
                'UPDATE jobs SET status = ?, run_id = ?, updated_at = ? WHERE job_id = ?',
                (JobStatus.RUNNING.value, run_id, now, row['job_id']),
            )
            claimed = conn.execute('SELECT * FROM jobs WHERE job_id = ?', (row['job_id'],)).fetchone()
            return self._row_to_dict(claimed, include_signature=True)

    def update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        run_id: Optional[str] = None,
        error_message: Optional[str] = None,
        output_dir: Optional[str] = None,
        progress_info: Optional[Dict[str, Any]] = None,
        exported_files: Optional[Dict[str, str]] = None,
    ) -> bool:
        now = self._now_iso()
        updates = ["status = ?", "updated_at = ?"]
        params: List[Any] = [status.value, now]

        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)
        if output_dir is not None:
            updates.append("output_dir = ?")
            params.append(output_dir)
        if progress_info is not None:
            updates.append("progress_info = ?")
            params.append(json.dumps({**progress_info, "last_activity": now}))
        if exported_files is not None:
            updates.append("exported_files_json = ?")
            params.append(json.dumps(exported_files, ensure_ascii=False))

        params.append(job_id)
        sql = f"UPDATE jobs SET {', '.join(updates)} WHERE job_id = ?"
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)

        # Defense in depth: Never overwrite cancelled or pausing/paused states when completing or running
        if status in (JobStatus.COMPLETED, JobStatus.RUNNING):
            sql += " AND status NOT IN ('cancelled', 'cancelling', 'paused', 'pausing')"
        elif run_id is None and status != JobStatus.CANCELLED:
            sql += " AND status NOT IN ('cancelled', 'cancelling')"

        with self._get_connection() as conn:
            cursor = conn.execute(sql, params)
            return cursor.rowcount > 0

    def cancel_job(self, job_id: str, to_cancelling: bool = False) -> bool:
        """
        Cancels a queued or running job.
        If to_cancelling is True and job is running/pausing, transitions to CANCELLING.
        Returns False if job does not exist or is already completed or cancelled.
        """
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status, run_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return False
            st = row["status"]
            if st in (JobStatus.COMPLETED.value, JobStatus.CANCELLED.value):
                return False
            if st == JobStatus.CANCELLING.value:
                return True
            if st in (JobStatus.QUEUED.value, JobStatus.PAUSED.value):
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        error_message = 'Cancelled by user',
                        run_id = NULL,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (JobStatus.CANCELLED.value, now, job_id),
                )
                return True
            if st in (JobStatus.RUNNING.value, JobStatus.PAUSING.value):
                target_status = JobStatus.CANCELLING.value if to_cancelling else JobStatus.CANCELLED.value
                msg = 'Cancelling by user...' if to_cancelling else 'Cancelled by user'
                run_val = row["run_id"] if to_cancelling else None
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        error_message = ?,
                        run_id = ?,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (target_status, msg, run_val, now, job_id),
                )
                return True
            return False

    def pause_job(self, job_id: str) -> bool:
        """
        Pauses a job.
        If QUEUED: sets status to PAUSED and clears run_id.
        If RUNNING: transitions to PAUSING.
        """
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status, run_id FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return False
            st = row["status"]
            if st in (JobStatus.COMPLETED.value, JobStatus.CANCELLED.value, JobStatus.FAILED.value):
                return False
            if st in (JobStatus.PAUSED.value, JobStatus.PAUSING.value):
                return True
            if st == JobStatus.QUEUED.value:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        run_id = NULL,
                        error_message = 'Paused by user',
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (JobStatus.PAUSED.value, now, job_id),
                )
                return True
            if st == JobStatus.RUNNING.value:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (JobStatus.PAUSING.value, now, job_id),
                )
                return True
            return False

    def resume_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Resumes a paused or pausing job by transitioning to QUEUED.
        """
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None
            st = row["status"]
            if st in (JobStatus.RUNNING.value, JobStatus.QUEUED.value):
                return self._row_to_dict(row, include_signature=False)
            if st not in (JobStatus.PAUSED.value, JobStatus.PAUSING.value):
                raise ValueError(f"Cannot resume job in '{st}' state (only allowed for paused jobs)")

            conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    run_id = NULL,
                    error_message = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (JobStatus.QUEUED.value, now, job_id),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_dict(updated, include_signature=False)

    def retry_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Independent retry for failed, cancelled, cancelling, paused, or needs_refresh jobs.
        Re-queues using existing parameters and cached audio if available, without requiring fresh signature.
        """
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None
            st = row["status"]
            if st in (JobStatus.RUNNING.value, JobStatus.QUEUED.value):
                return self._row_to_dict(row, include_signature=False)
            if st == JobStatus.COMPLETED.value:
                raise ValueError("Cannot retry completed job")

            conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    error_message = NULL,
                    progress_info = ?,
                    run_id = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (JobStatus.QUEUED.value, json.dumps({"stage": "queued", "percent": 0.0, "message": "重新排队重试"}), now, job_id),
            )
            updated = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_dict(updated, include_signature=False)

    def get_job(self, job_id: str, include_signature: bool = False) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None
            return self._row_to_dict(row, include_signature=include_signature)

    def list_jobs(self, limit: Optional[int] = None, offset: int = 0) -> List[Dict[str, Any]]:
        query = """
            SELECT * FROM jobs
            ORDER BY
                CASE status
                    WHEN 'running' THEN 1
                    WHEN 'pausing' THEN 2
                    WHEN 'cancelling' THEN 3
                    WHEN 'queued' THEN 4
                    ELSE 5
                END ASC,
                updated_at DESC,
                created_at DESC
        """
        params: List[Any] = []
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset > 0:
            query += " LIMIT -1 OFFSET ?"
            params.append(offset)

        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_dict(r, include_signature=False) for r in rows]

    def export_job(self, job_id: str, output_dir: str, keep_audio: bool = False) -> Dict[str, Any]:
        """
        Re-exports completed job results to a new directory without re-running ASR.
        Uses existing segments and export_job_results with collision protection.
        """
        from tongji_clipper.exporter import export_job_results

        job = self.get_job(job_id, include_signature=False)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        if job["status"] != JobStatus.COMPLETED.value:
            raise ValueError(f"Job {job_id} is not completed (current status: {job['status']})")

        src_dir = Path(job.get("output_dir", ""))
        if not src_dir.is_dir():
            raise ValueError(f"Original output directory does not exist: {src_dir}")

        dest_dir = Path(output_dir).resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Load segments from exported_files or checkpoint
        segments = None
        seg_candidates: List[Path] = []
        exp_files = job.get("exported_files", {})
        if exp_files.get("segments"):
            seg_candidates.append(Path(exp_files["segments"]))
        seg_candidates.extend(sorted(src_dir.glob("*.segments.json")))
        seg_candidates.append(src_dir / "checkpoint.json")

        for cand in seg_candidates:
            if cand.is_file():
                try:
                    data = json.loads(cand.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        segments = data
                        break
                    elif isinstance(data, dict) and "segments" in data:
                        segments = data["segments"]
                        break
                    elif isinstance(data, dict) and "raw_chunks" in data:
                        segs = []
                        for ch in data["raw_chunks"]:
                            segs.extend(ch.get("segments", []))
                        if segs:
                            segments = segs
                            break
                except Exception:
                    pass

        if not segments:
            raise ValueError(f"No transcription segments found to export for job {job_id}")

        timings = {"acquisition": 0.0, "asr": 0.0, "export": 0.0, "total": 0.0}
        result_paths = export_job_results(job_info=job, segments=segments, output_dir=dest_dir, timings=timings)

        # Protect and export raw file if available in src_dir
        if "raw" not in result_paths:
            src_raw: Optional[Path] = None
            job_exp_files = job.get("exported_files") or {}
            if job_exp_files.get("raw") and Path(job_exp_files["raw"]).is_file():
                src_raw = Path(job_exp_files["raw"])
            else:
                raw_cands = sorted(src_dir.glob("*.raw.json"))
                if raw_cands:
                    src_raw = raw_cands[0]

            md_stem = Path(result_paths["markdown"]).stem
            target_raw = dest_dir / f"{md_stem}.raw.json"
            if src_raw and src_raw.is_file():
                if src_raw.resolve() != target_raw.resolve():
                    shutil.copy2(src_raw, target_raw)
                result_paths["raw"] = str(target_raw)
            elif (src_dir / "checkpoint.json").is_file():
                try:
                    cp_data = json.loads((src_dir / "checkpoint.json").read_text(encoding="utf-8"))
                    from tongji_clipper.exporter import get_library_versions, write_atomic
                    write_atomic(target_raw, json.dumps({
                        "raw_chunks": cp_data.get("raw_chunks", []),
                        "config": job.get("config", {}),
                        "library_versions": get_library_versions(),
                    }, ensure_ascii=False, indent=2))
                    result_paths["raw"] = str(target_raw)
                except Exception:
                    pass

        warning = None
        if keep_audio:
            src_wav = src_dir / "audio_16k_mono.wav"
            if src_wav.is_file():
                dest_wav = dest_dir / f"{Path(result_paths['markdown']).stem}.audio.wav"
                revision = 1
                while dest_wav.exists():
                    dest_wav = dest_dir / f"{Path(result_paths['markdown']).stem}.audio_rev{revision}.wav"
                    revision += 1
                if src_wav.resolve() != dest_wav.resolve():
                    shutil.copy2(src_wav, dest_wav)
                result_paths["audio"] = str(dest_wav)
            else:
                warning = "原始音频已在先前任务中按策略清理，已导出文本及字幕"

        files = list(result_paths.values())
        if len(files) == 0:
            raise ValueError("Zero artifacts exported")

        return {
            "status": "ok",
            "job_id": job_id,
            "output_dir": str(dest_dir),
            "files": files,
            "exported_files": result_paths,
            "audio_exported": bool(keep_audio and warning is None),
            "warning": warning,
        }

    def cleanup_cache(self, job_id: Optional[str] = None) -> int:
        """
        Safely deletes reproducible cache files (audio_16k_mono.wav, chunk_*.wav).
        NEVER deletes checkpoint.json, user markdown, srt, raw JSON, or inputs.
        Refuses to clean active/pending jobs (running, prefetch, queued, pausing, cancelling).
        Requires strict task directory ownership matching {course_id}_{sub_id}_{job_id}.
        Returns total bytes freed.
        """
        cleanable_statuses = (
            JobStatus.COMPLETED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
            JobStatus.PAUSED.value,
        )
        total_freed = 0
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if job_id:
                row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                if not row:
                    raise ValueError(f"Job {job_id} not found")
                st = row["status"]
                if st not in cleanable_statuses:
                    raise ValueError(f"Cannot clean cache for active/pending job '{job_id}' in state '{st}'")
                rows = [row]
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status IN (?, ?, ?, ?)",
                    cleanable_statuses,
                ).fetchall()

            for r in rows:
                out_dir = r["output_dir"]
                if not out_dir:
                    continue
                original_dir = Path(out_dir).absolute()
                import stat
                if any(p.is_symlink() or (getattr(p.stat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                       for p in (original_dir, *original_dir.parents) if p.exists()):
                    continue
                p_dir = original_dir.resolve()
                if not p_dir.is_dir() or p_dir == p_dir.parent:
                    continue

                # Verify ownership: folder name must strictly equal {course_id}_{sub_id}_{job_id}
                safe_course = assert_safe_id(r["course_id"], "course_id")
                safe_sub = assert_safe_id(r["sub_id"], "sub_id")
                expected_name = f"{safe_course}_{safe_sub}_{r['job_id']}"
                if p_dir.name != expected_name:
                    continue

                # Do not delete if p_dir is or contains user's source local audio file
                if r["tenant_code"] == "local" or str(r["source_url"]).startswith("file://"):
                    local_file = str(r["signed_url"] or "")
                    if local_file and Path(local_file).resolve().parent == p_dir:
                        continue

                wav_file = p_dir / "audio_16k_mono.wav"
                marker_file = p_dir / "acquisition.json"
                if wav_file.is_file():
                    try:
                        size = wav_file.stat().st_size
                        wav_file.unlink()
                        total_freed += size
                        if marker_file.is_file():
                            marker_file.unlink()
                    except OSError:
                        pass

                for chunk_file in p_dir.glob("chunk_*.wav"):
                    try:
                        size = chunk_file.stat().st_size
                        chunk_file.unlink()
                        total_freed += size
                    except OSError:
                        pass

        return total_freed

    def get_total_cache_bytes(self) -> int:
        """Returns total reproducible wav cache bytes across all jobs."""
        total = 0
        with self._get_connection() as conn:
            rows = conn.execute("SELECT output_dir FROM jobs WHERE output_dir IS NOT NULL").fetchall()
        for r in rows:
            out_dir = r["output_dir"]
            if not out_dir:
                continue
            p_dir = Path(out_dir)
            if not p_dir.is_dir() or p_dir.is_symlink():
                continue
            wav_file = p_dir / "audio_16k_mono.wav"
            if wav_file.is_file():
                total += wav_file.stat().st_size
            for c in p_dir.glob("chunk_*.wav"):
                total += c.stat().st_size
        return total

    @staticmethod
    def _enrich_job_dict(d: Dict[str, Any]) -> Dict[str, Any]:
        out_dir = d.get("output_dir")
        audio_ready = False
        cache_bytes = 0
        if out_dir:
            try:
                p_dir = Path(out_dir)
                if p_dir.is_dir():
                    wav_file = p_dir / "audio_16k_mono.wav"
                    marker_file = p_dir / "acquisition.json"
                    if wav_file.is_file() and marker_file.is_file():
                        wav_size = wav_file.stat().st_size
                        if wav_size > 44:
                            marker_data = json.loads(marker_file.read_text(encoding="utf-8"))
                            rec_size = marker_data.get("size")
                            if rec_size == wav_size and marker_data.get("duration", 0) > 0 and marker_data.get("sample_rate") == 16000 and marker_data.get("channels") == 1:
                                audio_ready = True
                    if wav_file.is_file():
                        cache_bytes += wav_file.stat().st_size
                    for c in p_dir.glob("chunk_*.wav"):
                        cache_bytes += c.stat().st_size
            except Exception:
                pass

        st = d.get("status")
        is_completed = (st == JobStatus.COMPLETED.value)
        is_local = (d.get("tenant_code") == "local" or str(d.get("source_url", "")).startswith("file://"))
        if is_completed or is_local or audio_ready:
            page_required = False
        else:
            page_required = True

        d["audio_ready"] = audio_ready
        d["page_required"] = page_required
        d["cache_bytes"] = cache_bytes
        return d

    @staticmethod
    def _row_to_dict(row: sqlite3.Row, include_signature: bool = False) -> Dict[str, Any]:
        d = dict(row)
        if not include_signature and "signed_url" in d:
            del d["signed_url"]
        if "config_json" in d and d["config_json"]:
            try:
                d["config"] = json.loads(d["config_json"])
            except Exception:
                d["config"] = {}
        if "progress_info" in d and d["progress_info"]:
            try:
                d["progress_info"] = json.loads(d["progress_info"])
            except Exception:
                pass

        # Parse or discover exported_files
        exported_files = None
        if "exported_files_json" in d and d["exported_files_json"]:
            try:
                exported_files = json.loads(d["exported_files_json"])
            except Exception:
                pass

        out_dir = d.get("output_dir")
        if not exported_files and out_dir:
            p_dir = Path(out_dir)
            manifest = p_dir / "manifest.json"
            if manifest.is_file():
                try:
                    m_data = json.loads(manifest.read_text(encoding="utf-8"))
                    exported_files = m_data.get("exported_files")
                except Exception:
                    pass
            if not exported_files and p_dir.is_dir():
                course_id = d.get("course_id", "")
                sub_id = d.get("sub_id", "")
                clean_base = re.sub(r'[\\/*?:"<>|]', "_", f"{course_id}_{sub_id}")
                discovered = {}
                try:
                    for p in sorted(p_dir.iterdir(), key=lambda x: x.stat().st_mtime if x.is_file() else 0):
                        if not p.is_file() or not p.name.startswith(clean_base):
                            continue
                        if p.name.endswith(".md"):
                            discovered["markdown"] = str(p)
                        elif p.name.endswith(".srt"):
                            discovered["srt"] = str(p)
                        elif p.name.endswith(".segments.json"):
                            discovered["segments"] = str(p)
                        elif p.name.endswith(".raw.json"):
                            discovered["raw"] = str(p)
                except Exception:
                    pass
                if discovered:
                    exported_files = discovered

        d["exported_files"] = exported_files or {}

        if not include_signature:
            Database._enrich_job_dict(d)
        return d
