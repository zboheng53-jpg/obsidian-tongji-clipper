"""Regression tests for:
1. Immediate retry during cancellation (race condition prevention).
2. Late progress from old execution cannot pollute new execution (run_id isolation).
3. Pause, resume, and checkpoint continuity.
4. Cancellation of queued jobs.
5. Independent retry API reusing cached audio without original source.
6. Distinction between signature refresh and cache retry.
7. Clean shutdown and crash recovery.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave

from tongji_clipper.asr import ASREngine
from tongji_clipper.db import Database
from tongji_clipper.models import CapturePayload, JobConfig, JobStatus, MediaStream
from tongji_clipper.worker.runner import JobRunner


def create_test_wav(path: Path, duration_sec: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        num_frames = int(16000 * duration_sec)
        w.writeframes(b"\x00\x00" * num_frames)


class TestCancellationAndIsolation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = str(self.root / "test.db")
        self.db = Database(self.db_path)
        self.audio_path = self.root / "sample.wav"
        create_test_wav(self.audio_path, duration_sec=2.0)
        self.capture = CapturePayload.create_local(str(self.audio_path))
        self.config = JobConfig(output_dir=str(self.root / "output"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_queue_cancellation(self):
        """Cancelling a queued job immediately transitions to CANCELLED and is not claimed."""
        sub = self.db.submit_job(self.capture, self.capture.media_streams[0].stream_id, self.config)
        job_id = sub["job_id"]
        self.assertEqual(self.db.get_job(job_id)["status"], JobStatus.QUEUED.value)

        runner = JobRunner(self.db)
        ok = runner.cancel_job(job_id)
        self.assertTrue(ok)

        job = self.db.get_job(job_id)
        self.assertEqual(job["status"], JobStatus.CANCELLED.value)
        self.assertEqual(job["error_message"], "Cancelled by user")

        # Next claimed job should be None
        self.assertIsNone(self.db.get_next_queued_job())

    def test_immediate_retry_during_cancellation(self):
        """
        When a running job is cancelled, it enters CANCELLING.
        If a retry is submitted while it is CANCELLING, the job is requeued.
        When the old run finishes unwinding, it MUST NOT overwrite the queued retry.
        """
        entered_asr = threading.Event()
        finish_old_run = threading.Event()

        def slow_transcribe(*args, **kwargs):
            entered_asr.set()
            finish_old_run.wait(timeout=5.0)
            return [{"start": 0.0, "end": 1.0, "text": "Old run"}]

        engine = ASREngine(transcribe_fn=slow_transcribe)
        runner = JobRunner(self.db, engine)

        sub = self.db.submit_job(self.capture, self.capture.media_streams[0].stream_id, self.config)
        job_id = sub["job_id"]

        claimed = self.db.get_next_queued_job()
        self.assertIsNotNone(claimed)
        old_run_id = claimed["run_id"]

        # Run old execution in background thread
        worker_thread = threading.Thread(target=runner._process_single_job, args=(claimed,))
        worker_thread.start()

        # Wait until ASR starts
        self.assertTrue(entered_asr.wait(timeout=3.0))

        # Cancel while running -> DB status becomes CANCELLING
        runner.cancel_job(job_id)
        cancelling_job = self.db.get_job(job_id)
        self.assertEqual(cancelling_job["status"], JobStatus.CANCELLING.value)

        # User immediately calls retry
        retried = self.db.retry_job(job_id)
        self.assertEqual(retried["status"], JobStatus.QUEUED.value)
        # run_id must be cleared in DB
        self.assertIsNone(self.db.get_job(job_id).get("run_id"))

        # Release old run to finish unwinding
        finish_old_run.set()
        worker_thread.join(timeout=3.0)

        # Crucial assertion: Old run's teardown must NOT overwrite the retried job!
        after_old_exit = self.db.get_job(job_id)
        self.assertEqual(after_old_exit["status"], JobStatus.QUEUED.value)

        # Now runner can pick up the retry as a new run
        engine._mock_transcribe_fn = lambda *args, **kwargs: [{"start": 0.0, "end": 1.0, "text": "New run"}]
        new_claim = self.db.get_next_queued_job()
        self.assertIsNotNone(new_claim)
        self.assertNotEqual(new_claim["run_id"], old_run_id)

        runner._process_single_job(new_claim)
        final_job = self.db.get_job(job_id)
        self.assertEqual(final_job["status"], JobStatus.COMPLETED.value)

    def test_late_progress_rejected_by_run_id(self):
        """Late progress update from an old execution generation is rejected."""
        sub = self.db.submit_job(self.capture, self.capture.media_streams[0].stream_id, self.config)
        job_id = sub["job_id"]

        # Claim as Run 1
        claim_1 = self.db.get_next_queued_job()
        run_1_id = claim_1["run_id"]

        # Cancel Run 1 and retry
        self.db.cancel_job(job_id)
        self.db.retry_job(job_id)
        claim_2 = self.db.get_next_queued_job()
        self.assertIsNotNone(claim_2)
        run_2_id = claim_2["run_id"]
        self.assertNotEqual(run_1_id, run_2_id)

        # Set fresh progress for Run 2
        self.db.update_job_status(
            job_id,
            JobStatus.RUNNING,
            run_id=run_2_id,
            progress_info={"stage": "transcription", "current_chunk": 2, "message": "Run 2 fresh"},
        )

        # Old run 1 attempts late progress update
        ok = self.db.update_job_status(
            job_id,
            JobStatus.RUNNING,
            run_id=run_1_id,
            progress_info={"stage": "polluted", "message": "Run 1 late pollution"},
        )
        self.assertFalse(ok)

        # DB must still contain Run 2's progress
        job = self.db.get_job(job_id)
        self.assertEqual(job["progress_info"]["stage"], "transcription")
        self.assertEqual(job["progress_info"]["message"], "Run 2 fresh")

    def test_pause_and_resume_with_checkpoint(self):
        """
        Pausing a job saves completed chunks in checkpoint and sets status to PAUSED.
        Resuming requeues and completes from checkpoint without re-transcribing chunk 1.
        """
        create_test_wav(self.audio_path, duration_sec=580.0)
        cap = CapturePayload.create_local(str(self.audio_path))
        cfg = JobConfig(chunk_duration=300.0, chunk_overlap=10.0, output_dir=str(self.root / "output"))

        calls = []

        def mock_transcribe(audio_path, start, end, lang):
            calls.append(start)
            if start == 0:
                # Trigger pause while chunk 1 finishes
                runner.pause_job(job_id)
            return [{"start": 0.0, "end": 1.0, "text": f"Chunk at {start}"}]

        engine = ASREngine(transcribe_fn=mock_transcribe)
        runner = JobRunner(self.db, engine)

        sub = self.db.submit_job(cap, cap.media_streams[0].stream_id, cfg)
        job_id = sub["job_id"]

        claimed = self.db.get_next_queued_job()
        runner._process_single_job(claimed)

        # Job must be paused
        paused_job = self.db.get_job(job_id)
        self.assertEqual(paused_job["status"], JobStatus.PAUSED.value)

        # Checkpoint should exist with completed chunk 0
        cp_file = Path(paused_job["output_dir"] or (self.root / "output" / f"local_{cap.sub_id}_{job_id}")) / "checkpoint.json"
        self.assertTrue(cp_file.exists())
        cp_data = json.loads(cp_file.read_text(encoding="utf-8"))
        self.assertEqual(len(cp_data["raw_chunks"]), 1)
        self.assertEqual(cp_data["next_start"], 290.0)

        # Resume job
        resumed = runner.resume_job(job_id)
        self.assertEqual(resumed["status"], JobStatus.QUEUED.value)

        # Process resumed job
        claim_2 = self.db.get_next_queued_job()
        runner._process_single_job(claim_2)

        final_job = self.db.get_job(job_id)
        self.assertEqual(final_job["status"], JobStatus.COMPLETED.value)

        # Chunk 0 was transcribed once; second run only transcribed chunk 290!
        self.assertEqual(calls, [0, 290])

    def test_independent_retry_reuses_cached_audio_without_source(self):
        """Retry reuses verified cached audio on disk even if local/remote source disappears."""
        cap = CapturePayload.create_local(str(self.audio_path))
        cfg = JobConfig(output_dir=str(self.root / "output"))

        engine = ASREngine(transcribe_fn=lambda *args: (_ for _ in ()).throw(RuntimeError("Transient error")))
        runner = JobRunner(self.db, engine)

        sub = self.db.submit_job(cap, cap.media_streams[0].stream_id, cfg)
        job_id = sub["job_id"]

        runner._process_single_job(self.db.get_next_queued_job())
        self.assertEqual(self.db.get_job(job_id)["status"], JobStatus.FAILED.value)

        # Delete source audio
        self.audio_path.unlink()
        self.assertFalse(self.audio_path.exists())

        # Successful retry with fixed engine
        engine._mock_transcribe_fn = lambda *args: [{"start": 0.0, "end": 1.0, "text": "Cached replay"}]
        retried = self.db.retry_job(job_id)
        self.assertEqual(retried["status"], JobStatus.QUEUED.value)

        runner._process_single_job(self.db.get_next_queued_job())
        completed = self.db.get_job(job_id)
        self.assertEqual(completed["status"], JobStatus.COMPLETED.value)

    def test_distinct_signature_refresh_vs_cache_retry(self):
        """Differentiates refresh_signature (requires fresh valid URL) from retry_job (offline replay)."""
        remote_cap = CapturePayload(
            schema_version=1,
            tenant_code="tongji",
            course_id="MATH01",
            sub_id="01",
            source_url="https://look.tongji.edu.cn/livingroom?course_id=MATH01&sub_id=01&tenant_code=tongji",
            media_streams=[
                MediaStream(
                    stream_id="stream_main",
                    signed_url="https://look.tongji.edu.cn/play/video.mp4?sig=old",
                )
            ],
        )
        sub = self.db.submit_job(remote_cap, "stream_main", self.config)
        job_id = sub["job_id"]

        self.db.update_job_status(job_id, JobStatus.NEEDS_REFRESH, error_message="403 Forbidden")

        # 1. Invalid signature refresh is rejected
        with self.assertRaises(ValueError):
            self.db.refresh_signature(job_id, "https://malicious.site/play/video.mp4?sig=evil")

        # 2. Valid signature refresh updates signed_url and queues
        new_url = "https://look.tongji.edu.cn/play/video.mp4?sig=fresh_123"
        refreshed = self.db.refresh_signature(job_id, new_url)
        self.assertEqual(refreshed["status"], JobStatus.QUEUED.value)
        internal = self.db.get_job(job_id, include_signature=True)
        self.assertEqual(internal["signed_url"], new_url)

        # 3. Retry on cancelled job works without providing any URL
        self.db.update_job_status(job_id, JobStatus.CANCELLED)
        retried = self.db.retry_job(job_id)
        self.assertEqual(retried["status"], JobStatus.QUEUED.value)
        self.assertEqual(retried["progress_info"]["stage"], "queued")


if __name__ == "__main__":
    unittest.main()
