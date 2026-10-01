"""SQLite persistent queue, idempotency, signature isolation, and crash recovery."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Dict, List, Optional

from tongji_clipper.config import get_db_path
from tongji_clipper.models import (
    CapturePayload,
    JobConfig,
    JobStatus,
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
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

            if row:
                current_status = row["status"]
                if current_status == JobStatus.COMPLETED.value:
                    return self._row_to_dict(row, include_signature=False)
                elif current_status == JobStatus.RUNNING.value:
                    return self._row_to_dict(row, include_signature=False)
                elif current_status == JobStatus.QUEUED.value:
                    # Update signed_url of queued job with newer signature
                    conn.execute(
                        "UPDATE jobs SET signed_url = ?, updated_at = ? WHERE job_id = ?",
                        (target_stream.signed_url, now, row["job_id"]),
                    )
                    updated = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
                    return self._row_to_dict(updated, include_signature=False)
                elif current_status in (JobStatus.NEEDS_REFRESH.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value):
                    # Update signed_url and requeue
                    conn.execute(
                        """
                        UPDATE jobs
                        SET signed_url = ?,
                            time_offset = ?,
                            status = ?,
                            error_message = NULL,
                            progress_info = NULL,
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
        Jobs that were left in 'running' state are NOT silently marked complete.
        Reset them to 'queued' so they resume cleanly.
        """
        now = self._now_iso()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    error_message = 'Interrupted by worker restart; requeued',
                    updated_at = ?
                WHERE status = ?
                """,
                (JobStatus.QUEUED.value, now, JobStatus.RUNNING.value),
            )
            return cursor.rowcount

    def get_next_queued_job(self) -> Optional[Dict[str, Any]]:
        """Picks the oldest queued job (FIFO). Includes signed_url for private worker processing."""
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
            conn.execute('UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?', (JobStatus.RUNNING.value, self._now_iso(), row['job_id']))
            claimed = conn.execute('SELECT * FROM jobs WHERE job_id = ?', (row['job_id'],)).fetchone()
            return self._row_to_dict(claimed, include_signature=True)

    def update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        error_message: Optional[str] = None,
        output_dir: Optional[str] = None,
        progress_info: Optional[Dict[str, Any]] = None,
    ) -> None:
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
            params.append(json.dumps(progress_info))

        params.append(job_id)
        sql = f"UPDATE jobs SET {', '.join(updates)} WHERE job_id = ?"
        if status != JobStatus.CANCELLED:
            sql += " AND status != 'cancelled'"
        with self._get_connection() as conn:
            conn.execute(sql, params)

    def cancel_job(self, job_id: str) -> bool:
        """
        Cancels a queued or running job.
        Returns False if job does not exist or is already completed.
        """
        now = self._now_iso()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return False
            if row["status"] == JobStatus.COMPLETED.value:
                return False
            if row["status"] == JobStatus.CANCELLED.value:
                return False

            conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    error_message = 'Cancelled by user',
                    updated_at = ?
                WHERE job_id = ?
                """,
                (JobStatus.CANCELLED.value, now, job_id),
            )
            return True

    def get_job(self, job_id: str, include_signature: bool = False) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return None
            return self._row_to_dict(row, include_signature=include_signature)

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_dict(r, include_signature=False) for r in rows]

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
        return d
