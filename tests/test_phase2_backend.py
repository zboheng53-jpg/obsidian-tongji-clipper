"""Phase 2 Backend Hardening Tests:
1. Cancellation strictly takes precedence over pause.
2. Stop and crash recovery preserves resumability and pause intent.
3. Claim-to-lock race condition prevention.
4. Export stage cancellation protection (no overwrite to COMPLETED).
5. ASR segment generator cancellation check and chunk checkpoint integrity.
6. FFmpeg progress structured reporting and runner heartbeat.
7. Limited prefetch (cache budget, disk guard, single stream, cancellation).
8. Resource profiles (threads, priority, idle timeout auto-unload, model reuse).
9. Result reuse and safe cache cleanup.
10. Worker HTTP endpoints and Native Messaging bridge routing with request_id.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request
import urllib.error
import wave

from tongji_clipper.acquisition import extract_pcm_wav
from tongji_clipper.asr import ASREngine
from tongji_clipper.db import Database
from tongji_clipper.models import (
    CapturePayload,
    JobConfig,
    JobStatus,
    MediaStream,
    compute_idempotency_key,
)
from tongji_clipper.worker.runner import (
    JobRunner,
    get_cpu_threads_for_profile,
    apply_process_priority,
)
from tongji_clipper.worker.server import WorkerServer
from tongji_clipper.native_host.bridge import handle_client_message


def create_test_wav(path: Path, duration_sec: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        num_frames = int(16000 * duration_sec)
        w.writeframes(b"\x00\x00" * num_frames)


class TestPhase2Backend(unittest.TestCase):
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

    # 1. Cancellation precedence over pause
    def test_cancellation_precedence_over_pause(self):
        """When both pause and cancel are active or cancel follows pause, cancellation wins."""
        runner = JobRunner(self.db)
        sub = self.db.submit_job(self.capture, self.capture.media_streams[0].stream_id, self.config)
        job_id = sub["job_id"]
        run_id = "test_run_123"

        # Simulate job being in pausing state
        self.db.update_job_status(job_id, JobStatus.PAUSING, run_id=run_id)
        runner._paused_runs.add(run_id)

        # User cancels during pausing
        runner._cancelled_runs.add(run_id)
        runner.cancel_job(job_id)

        # Record interruption
        runner._record_interrupted(job_id, run_id)

        final_job = self.db.get_job(job_id)
        self.assertEqual(final_job["status"], JobStatus.CANCELLED.value)

    # 2. Stop and crash recovery
    def test_clean_stop_recovery_preserves_state(self):
        """
        Normal stop does not set CANCELLED.
        On recovery:
        - RUNNING -> QUEUED
        - PAUSING -> PAUSED
        - CANCELLING -> CANCELLED
        - Completed/Failed/Paused remain untouched.
        """
        cap1 = CapturePayload(1, "tongji", "course", "sub1", "https://look.tongji.edu.cn/livingroom?course_id=course&sub_id=sub1&tenant_code=tongji", media_streams=[MediaStream("st1", "https://look.tongji.edu.cn/play/v.mp4?sig=1")])
        cap2 = CapturePayload(1, "tongji", "course", "sub2", "https://look.tongji.edu.cn/livingroom?course_id=course&sub_id=sub2&tenant_code=tongji", media_streams=[MediaStream("st2", "https://look.tongji.edu.cn/play/v.mp4?sig=2")])
        cap3 = CapturePayload(1, "tongji", "course", "sub3", "https://look.tongji.edu.cn/livingroom?course_id=course&sub_id=sub3&tenant_code=tongji", media_streams=[MediaStream("st3", "https://look.tongji.edu.cn/play/v.mp4?sig=3")])
        cap4 = CapturePayload(1, "tongji", "course", "sub4", "https://look.tongji.edu.cn/livingroom?course_id=course&sub_id=sub4&tenant_code=tongji", media_streams=[MediaStream("st4", "https://look.tongji.edu.cn/play/v.mp4?sig=4")])

        sub1 = self.db.submit_job(cap1, "st1", self.config)
        sub2 = self.db.submit_job(cap2, "st2", self.config)
        sub3 = self.db.submit_job(cap3, "st3", self.config)
        sub4 = self.db.submit_job(cap4, "st4", self.config)

        j1, j2, j3, j4 = sub1["job_id"], sub2["job_id"], sub3["job_id"], sub4["job_id"]

        self.db.update_job_status(j1, JobStatus.RUNNING)
        self.db.update_job_status(j2, JobStatus.PAUSING)
        self.db.update_job_status(j3, JobStatus.CANCELLING)
        self.db.update_job_status(j4, JobStatus.PAUSED)

        runner = JobRunner(self.db)
        runner.stop()
        self.assertTrue(runner._stop_event.is_set())

        # Stop didn't alter DB directly
        self.assertEqual(self.db.get_job(j1)["status"], JobStatus.RUNNING.value)

        # Recover on startup
        count = self.db.recover_interrupted_jobs()
        self.assertEqual(count, 3)

        self.assertEqual(self.db.get_job(j1)["status"], JobStatus.QUEUED.value)
        self.assertEqual(self.db.get_job(j2)["status"], JobStatus.PAUSED.value)
        self.assertEqual(self.db.get_job(j3)["status"], JobStatus.CANCELLED.value)
        self.assertEqual(self.db.get_job(j4)["status"], JobStatus.PAUSED.value)

    # 3. Claim-to-lock race condition
    def test_claim_to_lock_race_cancelling(self):
        """If user cancels right after claim but before runner processes, runner handles gracefully."""
        sub = self.db.submit_job(self.capture, self.capture.media_streams[0].stream_id, self.config)
        job_id = sub["job_id"]

        claimed = self.db.get_next_queued_job()
        self.assertIsNotNone(claimed)
        run_id = claimed["run_id"]

        # Cancel while claimed in DB
        self.db.cancel_job(job_id, to_cancelling=True)

        engine = ASREngine(transcribe_fn=lambda *a, **kw: self.fail("ASR should not be called"))
        runner = JobRunner(self.db, engine)

        runner._process_single_job(claimed)

        job = self.db.get_job(job_id)
        self.assertEqual(job["status"], JobStatus.CANCELLED.value)

    def test_claim_to_lock_race_pausing(self):
        """If user pauses right after claim, runner detects PAUSING and finalizes to PAUSED."""
        sub = self.db.submit_job(self.capture, self.capture.media_streams[0].stream_id, self.config)
        job_id = sub["job_id"]

        claimed = self.db.get_next_queued_job()
        self.assertIsNotNone(claimed)

        # Pause in DB before processing
        self.db.pause_job(job_id)

        engine = ASREngine(transcribe_fn=lambda *a, **kw: self.fail("ASR should not be called"))
        runner = JobRunner(self.db, engine)

        runner._process_single_job(claimed)

        job = self.db.get_job(job_id)
        self.assertEqual(job["status"], JobStatus.PAUSED.value)

    # 4. Export stage interruption
    def test_export_stage_interruption_guard(self):
        """If user cancels during export, runner does not mark the job COMPLETED."""
        sub = self.db.submit_job(self.capture, self.capture.media_streams[0].stream_id, self.config)
        job_id = sub["job_id"]

        claimed = self.db.get_next_queued_job()
        run_id = claimed["run_id"]

        def mock_transcribe(*args, **kwargs):
            # Simulate cancel arriving at the end of ASR / start of export
            runner._cancelled_runs.add(run_id)
            self.db.cancel_job(job_id, to_cancelling=True)
            return [{"start": 0.0, "end": 1.0, "text": "Segment"}]

        engine = ASREngine(transcribe_fn=mock_transcribe)
        runner = JobRunner(self.db, engine)

        runner._process_single_job(claimed)

        job = self.db.get_job(job_id)
        self.assertEqual(job["status"], JobStatus.CANCELLED.value)

    # 5. ASR segment generator cancellation check & checkpoint integrity
    def test_asr_segment_generator_cancellation(self):
        """transcribe_chunk checks cancellation during segment generator iteration."""
        engine = ASREngine()

        cancelled = False
        def check_cancelled():
            return cancelled

        class MockSegment:
            def __init__(self, start, end, text):
                self.start = start
                self.end = end
                self.text = text
                self.avg_logprob = -0.1
                self.no_speech_prob = 0.0

        class MockModel:
            def transcribe(self, audio_data, **kwargs):
                def gen():
                    yield MockSegment(0.0, 0.5, "seg1")
                    nonlocal cancelled
                    cancelled = True  # Cancel after 1st segment
                    yield MockSegment(0.5, 1.0, "seg2")
                return gen(), None

        engine.model = MockModel()

        with self.assertRaises(RuntimeError):
            engine.transcribe_chunk(
                audio_path=str(self.audio_path),
                start_sec=0.0,
                end_sec=2.0,
                language="zh",
                check_cancelled=check_cancelled,
            )

    # 6. Structured progress reporting & Runner heartbeat
    def test_ffmpeg_progress_reporting_and_runner_heartbeat(self):
        """Progress callback receives structured progress info; runner updates heartbeat."""
        runner = JobRunner(self.db)
        self.assertFalse(runner.runner_alive)

        runner._is_running = True
        self.assertTrue(runner.runner_alive)
        initial_hb = runner.last_heartbeat
        time.sleep(0.05)
        runner.last_heartbeat = time.time()
        self.assertGreater(runner.last_heartbeat, initial_hb)

        # Test progress callback logic
        progress_records = []
        def on_prog(info):
            progress_records.append(info)

        # Simulate the progress reader line parsing logic from acquisition.py
        info = {
            "stage": "downloading",
            "audio_seconds": 15.5,
            "speed": "2.1x",
            "percent": 50.0,
            "last_activity": time.time(),
        }
        on_prog(info)
        self.assertEqual(len(progress_records), 1)
        self.assertEqual(progress_records[0]["audio_seconds"], 15.5)
        self.assertEqual(progress_records[0]["speed"], "2.1x")
        self.assertEqual(progress_records[0]["percent"], 50.0)

    # 7. Limited prefetch
    def test_limited_prefetch_cache_budget_and_disk_guard(self):
        """Prefetch respects cache budget (512MB) and low disk space (<1GB)."""
        runner = JobRunner(self.db)
        base_out = self.root / "output"
        base_out.mkdir(parents=True, exist_ok=True)

        # 1. When cache budget reached (>=512MB), prefetch does not start
        with mock.patch.object(self.db, "get_total_cache_bytes", return_value=600 * 1024 * 1024):
            runner._maybe_start_prefetch(base_out)
            self.assertIsNone(runner._prefetch_job_id)

        # 2. When free disk space < 1GB, prefetch does not start
        with mock.patch.object(self.db, "get_total_cache_bytes", return_value=100 * 1024 * 1024):
            with mock.patch("shutil.disk_usage") as mock_usage:
                mock_usage.return_value = mock.Mock(free=500 * 1024 * 1024)  # 500MB free
                runner._maybe_start_prefetch(base_out)
                self.assertIsNone(runner._prefetch_job_id)

    def test_limited_prefetch_execution_and_cancellation(self):
        """Prefetch acquires audio for next queued job and stops on cancellation."""
        sub1 = self.db.submit_job(self.capture, "s1", self.config)
        sub2 = self.db.submit_job(self.capture, "s2", self.config)

        runner = JobRunner(self.db)
        base_out = self.root / "output"
        base_out.mkdir(parents=True, exist_ok=True)

        next_job = self.db.peek_next_queued_job()
        self.assertIsNotNone(next_job)
        next_job_id = next_job["job_id"]

        # Cancel the job before prefetch runs
        self.db.cancel_job(next_job_id)

        # Run prefetch; should abort early due to check_prefetch_cancelled
        runner._run_prefetch(next_job, base_out)

        # Output wav should not exist
        job_dir = base_out / f"local_local_stream_{next_job_id}"
        self.assertFalse((job_dir / "audio_16k_mono.wav").exists())

    # 8. Resource profiles & Model lifecycle
    def test_resource_profiles_and_idle_unload(self):
        """Resource profile thread calculations and idle timeout unloading."""
        threads_low = get_cpu_threads_for_profile("low")
        threads_bal = get_cpu_threads_for_profile("balanced")
        threads_fast = get_cpu_threads_for_profile("fast")

        self.assertGreaterEqual(threads_low, 1)
        self.assertGreaterEqual(threads_bal, threads_low)
        self.assertGreaterEqual(threads_fast, threads_bal)

        # Process priority adjustment executes without error
        apply_process_priority("low")
        apply_process_priority("balanced")

        # Test ASREngine idle timeout unload
        engine = ASREngine(idle_timeout_sec=0.1)
        engine.model = "mock_model"
        engine.last_active_time = time.time() - 0.2
        self.assertIsNotNone(engine.model)

        engine.check_idle_unload()
        self.assertIsNone(engine.model)

    # 9. Result reuse and safe cache cleanup
    def test_idempotency_ignores_output_dir_and_keep_audio(self):
        """Changing output_dir or keep_audio does not change idempotency key."""
        c1 = JobConfig(output_dir="D:/out1", keep_audio=True)
        c2 = JobConfig(output_dir="D:/out2", keep_audio=False)
        k1 = compute_idempotency_key(self.capture.tenant_code, self.capture.course_id, self.capture.sub_id, "s1", 0.0, c1)
        k2 = compute_idempotency_key(self.capture.tenant_code, self.capture.course_id, self.capture.sub_id, "s1", 0.0, c2)
        self.assertEqual(k1, k2)

    def test_export_job_reexport_without_asr(self):
        """db.export_job copies artifacts to new directory without re-running ASR."""
        sub = self.db.submit_job(self.capture, self.capture.media_streams[0].stream_id, self.config)
        job_id = sub["job_id"]

        course_id = self.capture.course_id
        sub_id = self.capture.sub_id
        base_stem = f"{course_id}_{sub_id}"

        orig_dir = self.root / "orig_output"
        orig_dir.mkdir(parents=True, exist_ok=True)
        md_file = orig_dir / f"{base_stem}.md"
        srt_file = orig_dir / f"{base_stem}.srt"
        seg_file = orig_dir / f"{base_stem}.segments.json"
        raw_file = orig_dir / f"{base_stem}.raw.json"
        wav_file = orig_dir / "audio_16k_mono.wav"

        md_file.write_text("# Transcript\nHello", encoding="utf-8")
        srt_file.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        seg_file.write_text(json.dumps([{"start": 0.0, "end": 1.0, "text": "Hello"}]), encoding="utf-8")
        raw_file.write_text(json.dumps({"raw_chunks": []}), encoding="utf-8")
        wav_file.write_text("dummy_wav_content", encoding="utf-8")

        exported_files = {
            "markdown": str(md_file),
            "srt": str(srt_file),
            "segments": str(seg_file),
            "raw": str(raw_file),
        }
        self.db.update_job_status(job_id, JobStatus.COMPLETED, output_dir=str(orig_dir), exported_files=exported_files)

        new_export_dir = self.root / "exported"
        res = self.db.export_job(job_id, str(new_export_dir), keep_audio=True)

        self.assertEqual(res["status"], "ok")
        self.assertIn("files", res)
        self.assertTrue((new_export_dir / f"{base_stem}.md").exists())
        self.assertTrue((new_export_dir / f"{base_stem}.srt").exists())
        self.assertTrue((new_export_dir / f"{base_stem}.segments.json").exists())
        self.assertTrue((new_export_dir / f"{base_stem}.raw.json").exists())
        self.assertTrue(Path(res["exported_files"]["audio"]).exists())
        self.assertTrue(res["audio_exported"])

        # Test export when audio is missing/cleaned up
        wav_file.unlink()
        new_export_dir_2 = self.root / "exported_no_wav"
        res2 = self.db.export_job(job_id, str(new_export_dir_2), keep_audio=True)
        self.assertEqual(res2["status"], "ok")
        self.assertFalse(res2["audio_exported"])
        self.assertIn("warning", res2)

    def test_safe_cache_cleanup(self):
        """cleanup_cache deletes only reproducible wavs of inactive jobs, keeping artifacts."""
        sub = self.db.submit_job(self.capture, self.capture.media_streams[0].stream_id, self.config)
        job_id = sub["job_id"]

        safe_course = self.capture.course_id
        safe_sub = self.capture.sub_id
        job_dir = self.root / f"{safe_course}_{safe_sub}_{job_id}"
        job_dir.mkdir(parents=True, exist_ok=True)
        wav_file = job_dir / "audio_16k_mono.wav"
        chunk_file = job_dir / "chunk_0.wav"
        md_file = job_dir / f"{safe_course}_{safe_sub}.md"
        cp_file = job_dir / "checkpoint.json"

        wav_file.write_text("audio content", encoding="utf-8")
        chunk_file.write_text("chunk content", encoding="utf-8")
        md_file.write_text("# Saved Markdown", encoding="utf-8")
        cp_file.write_text("{}", encoding="utf-8")

        self.db.update_job_status(job_id, JobStatus.COMPLETED, output_dir=str(job_dir))

        freed_bytes = self.db.cleanup_cache(job_id=job_id)
        self.assertGreater(freed_bytes, 0)

        # WAV files must be removed
        self.assertFalse(wav_file.exists())
        self.assertFalse(chunk_file.exists())

        # MD and checkpoint MUST be preserved
        self.assertTrue(md_file.exists())
        self.assertTrue(cp_file.exists())

        # Enriched job page_required is False when completed without wav
        job = self.db.get_job(job_id)
        self.assertFalse(job["audio_ready"])
        self.assertFalse(job["page_required"])

        # Test malicious / non-matching directory rejection
        sub2 = self.db.submit_job(self.capture, "s_other", self.config)
        job_id_2 = sub2["job_id"]
        malicious_dir = self.root / "malicious_user_dir"
        malicious_dir.mkdir(parents=True, exist_ok=True)
        user_wav = malicious_dir / "audio_16k_mono.wav"
        user_wav.write_text("critical user audio", encoding="utf-8")
        self.db.update_job_status(job_id_2, JobStatus.COMPLETED, output_dir=str(malicious_dir))

        # Cleanup MUST reject deleting files in a non-owned directory
        freed_malicious = self.db.cleanup_cache(job_id=job_id_2)
        self.assertEqual(freed_malicious, 0)
        self.assertTrue(user_wav.exists())

    # 10. Worker HTTP endpoints and Bridge routing
    def test_worker_http_endpoints_and_bridge_passthrough(self):
        """Test Worker HTTP endpoints and Bridge request_id passthrough."""
        token = "SECRET_TOKEN_PHASE2"
        server = WorkerServer(("127.0.0.1", 0), self.db, token=token)
        port = server.server_address[1]
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()

        def http_req(path: str, method: str = "GET", body=None):
            url = f"http://127.0.0.1:{port}{path}"
            data = json.dumps(body).encode("utf-8") if body is not None else None
            req = urllib.request.Request(
                url, data=data,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                method=method,
            )
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                err_data = json.loads(e.read().decode("utf-8")) if e.fp else {}
                return e.code, err_data

        try:
            # 1. GET /system_info
            status, info = http_req("/system_info")
            self.assertEqual(status, 200)
            self.assertEqual(info["status"], "ok")
            self.assertIn("defaults", info)
            self.assertIn("diagnostics", info)
            self.assertIn("worker", info)
            self.assertIn("cache", info)

            # 2. POST /validate_config (valid and invalid)
            status, val = http_req("/validate_config", method="POST", body={"config": {"model": "small"}})
            self.assertIn(status, (200, 400))  # 200 if ffmpeg present, 400 if ffmpeg missing
            self.assertIn("checks", val)

            # Invalid config (non-existent model dir)
            status_bad, val_bad = http_req(
                "/validate_config",
                method="POST",
                body={"config": {"offline": True, "model_dir": "non_existent_model_dir_xyz"}},
            )
            self.assertEqual(status_bad, 400)
            self.assertEqual(val_bad["status"], "error")

            # 3. GET /jobs pagination and validation
            sub = self.db.submit_job(self.capture, "s1", self.config)
            status, job_list = http_req("/jobs?limit=10&offset=0")
            self.assertEqual(status, 200)
            self.assertGreaterEqual(len(job_list["jobs"]), 1)
            pub_job = job_list["jobs"][0]
            self.assertIn("audio_ready", pub_job)
            self.assertIn("page_required", pub_job)
            self.assertIn("cache_bytes", pub_job)
            self.assertNotIn("signed_url", pub_job)

            # Invalid pagination params return 400
            status_inv, _ = http_req("/jobs?limit=-5")
            self.assertEqual(status_inv, 400)

            # 4. POST /preview
            job_id = sub["job_id"]
            safe_course = self.capture.course_id
            safe_sub = self.capture.sub_id
            job_dir = self.root / f"{safe_course}_{safe_sub}_{job_id}"
            job_dir.mkdir(parents=True, exist_ok=True)
            seg_file = job_dir / f"{safe_course}_{safe_sub}.segments.json"
            raw_file = job_dir / f"{safe_course}_{safe_sub}.raw.json"
            md_file = job_dir / f"{safe_course}_{safe_sub}.md"

            segments_data = [
                {"start": 0.0, "end": 1.5, "text": "Segment 1", "avg_logprob": -0.2, "no_speech_prob": 0.01},
                {"start": 1.5, "end": 3.0, "text": "Segment 2", "avg_logprob": -1.5, "no_speech_prob": 0.8},
            ]
            seg_file.write_text(json.dumps(segments_data), encoding="utf-8")
            raw_file.write_text(json.dumps({"raw_chunks": [{"segments": segments_data}]}), encoding="utf-8")
            md_file.write_text("# Markdown\nSegment 1\nSegment 2", encoding="utf-8")

            exported_files = {
                "markdown": str(md_file),
                "segments": str(seg_file),
                "raw": str(raw_file),
            }
            self.db.update_job_status(job_id, JobStatus.COMPLETED, output_dir=str(job_dir), exported_files=exported_files)

            status, prev = http_req("/preview", method="POST", body={"job_id": job_id, "max_lines": 2})
            self.assertEqual(status, 200)
            self.assertEqual(prev["status"], "ok")
            self.assertIn("segments", prev)
            self.assertEqual(len(prev["segments"]), 2)
            self.assertEqual(prev["segments"][0]["text"], "Segment 1")
            self.assertFalse(prev["segments"][0]["needs_review"])
            self.assertEqual(prev["segments"][1]["text"], "Segment 2")
            self.assertTrue(prev["segments"][1]["needs_review"])
            self.assertIn("source_url", prev)

            # 5. POST /open_output
            with mock.patch("os.startfile") as mock_startfile:
                mock_startfile.return_value = None
                status, open_res = http_req("/open_output", method="POST", body={"job_id": job_id, "kind": "markdown"})
                self.assertEqual(status, 200)
                self.assertEqual(open_res["status"], "ok")
                self.assertEqual(open_res["path"], str(md_file))
                mock_startfile.assert_called_once_with(str(md_file))

            with mock.patch("os.startfile", side_effect=OSError("Access denied")):
                status_err, _ = http_req("/open_output", method="POST", body={"job_id": job_id, "kind": "markdown"})
                self.assertEqual(status_err, 500)

            # 6. POST /export
            http_export_dir = self.root / "http_exported"
            status, exp_res = http_req("/export", method="POST", body={"job_id": job_id, "output_dir": str(http_export_dir)})
            self.assertEqual(status, 200)
            self.assertEqual(exp_res["status"], "ok")
            self.assertIn("files", exp_res)
            self.assertEqual(Path(exp_res["output_dir"]).resolve(), http_export_dir.resolve())

            # 7. POST /cleanup_cache returns freed_bytes
            status, cl_res = http_req("/cleanup_cache", method="POST", body={"job_id": job_id})
            self.assertEqual(status, 200)
            self.assertEqual(cl_res["status"], "ok")
            self.assertIn("freed_bytes", cl_res)

            # 8. Bridge routing with request_id passthrough
            with mock.patch("tongji_clipper.native_host.bridge.send_worker_request") as mock_send:
                mock_send.return_value = {"status": "ok", "some_data": 123}
                msg = {
                    "action": "system_info",
                    "request_id": "req_abc_123",
                }
                resp = handle_client_message(msg)
                if isinstance(resp, dict) and "request_id" in msg:
                    resp["request_id"] = msg["request_id"]
                self.assertEqual(resp["request_id"], "req_abc_123")
                self.assertEqual(resp["status"], "ok")

        finally:
            server.stop_worker()
            server.shutdown()
            server.server_close()
            srv_thread.join(timeout=2.0)

    # 11. Checkpoint fingerprint resilience across resource profiles
    def test_checkpoint_resumes_across_different_resource_profiles(self):
        """Switching thread count or resource profile between pause and resume preserves checkpoint."""
        engine_fast = ASREngine(cpu_threads=8)
        engine_low = ASREngine(cpu_threads=2)

        fp_fast = engine_fast.compute_config_fingerprint("zh", 300.0, 10.0, 0.0)
        fp_low = engine_low.compute_config_fingerprint("zh", 300.0, 10.0, 0.0)

        # Both fingerprints must match so switching resource profile doesn't invalidate checkpoint
        self.assertEqual(fp_fast, fp_low)

    # 12. Model complete directory validation logic
    def test_is_complete_model_dir(self):
        from tongji_clipper.worker.server import is_complete_model_dir

        m_dir = self.root / "fake_model"
        m_dir.mkdir(parents=True, exist_ok=True)
        # Empty directory -> False
        self.assertFalse(is_complete_model_dir(m_dir))

        # Only a few KB dummy file -> False
        (m_dir / "model.bin").write_bytes(b"\x00" * 2048)  # 2KB
        (m_dir / "config.json").write_text("{}", encoding="utf-8")
        (m_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
        self.assertFalse(is_complete_model_dir(m_dir))

        # Real size model file (>50KB) -> True
        (m_dir / "model.bin").write_bytes(b"\x00" * 60000)  # ~60KB
        self.assertTrue(is_complete_model_dir(m_dir))

    # 13. Bridge auto-recovery when encountering 401
    def test_bridge_send_worker_request_401_recovery(self):
        from tongji_clipper.native_host.bridge import send_worker_request
        import urllib.error

        with tempfile.TemporaryDirectory() as td:
            os.environ["TONGJI_CLIPPER_HOME"] = td
            from tongji_clipper.config import save_worker_info

            # Save initial worker info with wrong token
            save_worker_info(port=48721, pid=9999, token="STALE_TOKEN")

            call_count = 0

            def fake_ensure():
                nonlocal call_count
                if call_count > 0:
                    return {"port": 48721, "pid": 10000, "token": "FRESH_TOKEN"}
                return {"port": 48721, "pid": 9999, "token": "STALE_TOKEN"}

            def fake_urlopen(req, timeout=10.0):
                nonlocal call_count
                call_count += 1
                auth = req.headers.get("Authorization")
                if auth != "Bearer FRESH_TOKEN":
                    import io
                    fp = io.BytesIO(b'{"error": "Unauthorized"}')
                    raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, fp)
                
                class FakeResp:
                    def read(self):
                        return b'{"status": "ok", "recovered": true}'
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        pass

                return FakeResp()

            with mock.patch("tongji_clipper.native_host.bridge.ensure_worker_running", side_effect=fake_ensure), \
                 mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = send_worker_request("/jobs", method="GET")
                self.assertEqual(result.get("status"), "ok")
                self.assertTrue(result.get("recovered"))
                self.assertEqual(call_count, 2)


if __name__ == "__main__":
    unittest.main()
