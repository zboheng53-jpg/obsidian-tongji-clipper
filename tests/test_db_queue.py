"""Tests for SQLite serial queue, idempotency, signature isolation, and crash recovery."""

import tempfile
import unittest
from pathlib import Path

from tongji_clipper.db import Database
from tongji_clipper.models import (
    CapturePayload,
    JobConfig,
    JobStatus,
    MediaStream,
    compute_idempotency_key,
)


class TestDatabaseAndQueue(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test_queue.db")
        self.db = Database(self.db_path)

        self.sample_capture = CapturePayload(
            schema_version=1,
            tenant_code="tongji",
            course_id="CS101",
            sub_id="lesson_01",
            source_url="https://look.tongji.edu.cn/livingroom?course_id=CS101&sub_id=lesson_01&tenant_code=tongji",
            course_title="计算机网络",
            sub_title="第一讲：物理层",
            lecturer_name="王老师",
            room_name="西教201",
            media_streams=[
                MediaStream(
                    stream_id="stream_main",
                    signed_url="https://look.tongji.edu.cn/play/stream.mp4?token=SECRET_SIG_123",
                    time_offset=0.0,
                )
            ],
        )
        self.config = JobConfig(model="small")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_idempotency_key_excludes_signature(self):
        """Ensures signature change does NOT alter idempotency key."""
        key1 = compute_idempotency_key(
            tenant_code="tongji",
            course_id="CS101",
            sub_id="lesson_01",
            stream_id="stream_main",
            time_offset=0.0,
            config=self.config,
        )

        key2 = compute_idempotency_key(
            tenant_code="tongji",
            course_id="CS101",
            sub_id="lesson_01",
            stream_id="stream_main",
            time_offset=0.0,
            config=self.config,
        )
        self.assertEqual(key1, key2)

    def test_signature_isolation_in_public_views(self):
        """Verifies signed_url is NEVER exposed in list_jobs or public get_job."""
        self.db.submit_job(self.sample_capture, "stream_main", self.config)

        # Public list
        jobs = self.db.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertNotIn("signed_url", jobs[0])

        # Public single job query
        job_public = self.db.get_job(jobs[0]["job_id"], include_signature=False)
        self.assertNotIn("signed_url", job_public)

        # Internal worker query includes signed_url
        job_internal = self.db.get_job(jobs[0]["job_id"], include_signature=True)
        self.assertIn("signed_url", job_internal)
        self.assertIn("SECRET_SIG_123", job_internal["signed_url"])

    def test_queued_job_signature_update_on_resubmit(self):
        """If a task is queued, submitting with a fresher signature updates the queued record."""
        sub1 = self.db.submit_job(self.sample_capture, "stream_main", self.config)
        job_id = sub1["job_id"]

        # Resubmit with fresh signature
        capture2 = CapturePayload(
            schema_version=1,
            tenant_code="tongji",
            course_id="CS101",
            sub_id="lesson_01",
            source_url="https://look.tongji.edu.cn/livingroom?course_id=CS101&sub_id=lesson_01&tenant_code=tongji",
            media_streams=[
                MediaStream(
                    stream_id="stream_main",
                    signed_url="https://look.tongji.edu.cn/play/stream.mp4?token=FRESH_TOKEN_789",
                    time_offset=0.0,
                )
            ],
        )
        sub2 = self.db.submit_job(capture2, "stream_main", self.config)
        self.assertEqual(sub2["job_id"], job_id)

        # Check internal signature updated
        internal = self.db.get_job(job_id, include_signature=True)
        self.assertIn("FRESH_TOKEN_789", internal["signed_url"])

    def test_crash_recovery_does_not_silently_complete_jobs(self):
        """Interrupted running jobs must be requeued, never silently dropped or completed."""
        sub = self.db.submit_job(self.sample_capture, "stream_main", self.config)
        job_id = sub["job_id"]

        # Simulate job running before crash
        self.db.update_job_status(job_id, JobStatus.RUNNING)
        job_before = self.db.get_job(job_id)
        self.assertEqual(job_before["status"], JobStatus.RUNNING.value)

        # Worker restarts and triggers recovery
        recovered_count = self.db.recover_interrupted_jobs()
        self.assertEqual(recovered_count, 1)

        job_after = self.db.get_job(job_id)
        # MUST be queued for resume, NOT completed!
        self.assertEqual(job_after["status"], JobStatus.QUEUED.value)
        self.assertIn("Interrupted by worker restart", job_after["error_message"])

    def test_signature_refresh_mechanism(self):
        """Simulate signature expiration (needs_refresh) and refreshing with new signature."""
        sub = self.db.submit_job(self.sample_capture, "stream_main", self.config)
        job_id = sub["job_id"]

        # Transition to needs_refresh
        self.db.update_job_status(job_id, JobStatus.NEEDS_REFRESH, error_message="403 Forbidden")
        job = self.db.get_job(job_id)
        self.assertEqual(job["status"], JobStatus.NEEDS_REFRESH.value)

        # Refresh signature
        new_url = "https://look.tongji.edu.cn/play/stream.mp4?token=NEW_FRESH_SIG_456"
        refreshed = self.db.refresh_signature(job_id, new_url)
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed["status"], JobStatus.QUEUED.value)

        # Verify internal private data has updated URL
        internal = self.db.get_job(job_id, include_signature=True)
        self.assertEqual(internal["signed_url"], new_url)

    def test_refresh_signature_forbidden_for_running_or_completed(self):
        """Refreshing signature on running or completed job must be rejected."""
        sub = self.db.submit_job(self.sample_capture, "stream_main", self.config)
        job_id = sub["job_id"]

        # Mark completed
        self.db.update_job_status(job_id, JobStatus.COMPLETED)
        with self.assertRaises(ValueError):
            self.db.refresh_signature(job_id, "https://look.tongji.edu.cn/play/stream.mp4?sig=new")

        # Mark running
        self.db.update_job_status(job_id, JobStatus.RUNNING)
        with self.assertRaises(ValueError):
            self.db.refresh_signature(job_id, "https://look.tongji.edu.cn/play/stream.mp4?sig=new")

    def test_cannot_cancel_completed_job(self):
        """Cancelling an already completed job returns False."""
        sub = self.db.submit_job(self.sample_capture, "stream_main", self.config)
        job_id = sub["job_id"]

        self.db.update_job_status(job_id, JobStatus.COMPLETED)
        ok = self.db.cancel_job(job_id)
        self.assertFalse(ok)
        job = self.db.get_job(job_id)
        self.assertEqual(job["status"], JobStatus.COMPLETED.value)


if __name__ == "__main__":
    unittest.main()
