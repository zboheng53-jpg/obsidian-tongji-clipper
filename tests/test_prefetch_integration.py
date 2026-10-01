"""Synthetic media and controlled races for prefetch ownership."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tongji_clipper.acquisition import extract_pcm_wav
from tongji_clipper.db import Database
from tongji_clipper.models import CapturePayload, JobConfig
from tongji_clipper.worker.runner import JobRunner
from tongji_clipper.asr import ASREngine
from tongji_clipper.models import compute_legacy_idempotency_key


class PrefetchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.wav'
        with wave.open(str(self.source), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'\0\0' * 16000)
        self.db = Database(str(self.root / 'queue.db'))
        capture = CapturePayload.create_local(str(self.source), title='Synthetic')
        self.job = self.db.submit_job(capture, capture.media_streams[0].stream_id,
                                      JobConfig(output_dir=str(self.root / 'successor')))
        self.runner = JobRunner(self.db)

    def test_real_prefetch_format_marker_and_successor_directory(self):
        self.runner._maybe_start_prefetch()
        self.assertIsNotNone(self.runner._prefetch_thread)
        self.runner._prefetch_thread.join(timeout=8)
        self.assertFalse(self.runner._prefetch_thread.is_alive())
        job = self.db.get_job(self.job['job_id'])
        self.assertEqual(Path(job['output_dir']).parent, self.root / 'successor')
        self.assertTrue(job['audio_ready'])
        self.assertEqual(job['status'], 'queued')
        self.assertFalse(list(Path(job['output_dir']).glob('*.part')))

    def test_cancel_retry_during_acquisition_cannot_publish_old_audio(self):
        def acquire(**kwargs):
            result = extract_pcm_wav(**kwargs)
            self.db.cancel_job(self.job['job_id'])
            self.db.retry_job(self.job['job_id'])
            return result
        with patch('tongji_clipper.worker.runner.extract_pcm_wav', side_effect=acquire):
            self.runner._run_prefetch(self.db.peek_next_queued_job())
        job = self.db.get_job(self.job['job_id'])
        self.assertEqual(job['status'], 'queued')
        self.assertFalse(job['audio_ready'])
        self.assertFalse((Path(job['output_dir']) / 'audio_16k_mono.wav').exists())

    def test_expired_old_prefetch_cannot_override_retry(self):
        from tongji_clipper.acquisition import SignatureExpiredError
        def expire(**kwargs):
            self.db.cancel_job(self.job['job_id'])
            self.db.retry_job(self.job['job_id'])
            raise SignatureExpiredError('synthetic expired source')
        with patch('tongji_clipper.worker.runner.extract_pcm_wav', side_effect=expire):
            self.runner._run_prefetch(self.db.peek_next_queued_job())
        self.assertEqual(self.db.get_job(self.job['job_id'])['status'], 'queued')

    def test_budget_crossing_discards_partial(self):
        def acquire(**kwargs):
            Path(kwargs['output_wav_path']).write_bytes(b'partial')
            with patch.object(self.db, 'get_total_cache_bytes', return_value=512 * 1024 * 1024):
                kwargs['on_progress']({'audio_seconds': 1})
            self.assertTrue(kwargs['check_cancelled']())
            return {}
        with patch('tongji_clipper.worker.runner.extract_pcm_wav', side_effect=acquire):
            self.runner._run_prefetch(self.db.peek_next_queued_job())
        job = self.db.get_job(self.job['job_id'])
        self.assertFalse(job['audio_ready'])
        self.assertFalse(list(Path(job['output_dir']).glob('*.part')))

    def test_runner_does_not_claim_after_prefetch_wait_timeout(self):
        class StillRunning:
            def is_alive(inner):
                return True
            def join(inner, timeout):
                self.runner._stop_event.set()
        self.runner._prefetch_thread = StillRunning()
        self.runner._prefetch_job_id = self.job['job_id']
        with patch.object(self.db, 'get_next_queued_job', wraps=self.db.get_next_queued_job) as claim:
            self.runner.run_loop()
        claim.assert_not_called()

    def test_ffmpeg_progress_uses_seconds_for_one_second_audio(self):
        progress = []
        extract_pcm_wav(str(self.source), str(self.root / 'verified.part'), on_progress=progress.append)
        self.assertTrue(progress)
        self.assertGreater(max(item['audio_seconds'] for item in progress), 0.5)
        self.assertLessEqual(max(item['audio_seconds'] for item in progress), 1.1)

    def test_unverified_audio_counts_as_cache_but_is_not_ready(self):
        self.runner._run_prefetch(self.db.peek_next_queued_job())
        job = self.db.get_job(self.job['job_id'])
        marker = Path(job['output_dir']) / 'acquisition.json'
        marker.write_text('{}', encoding='utf-8')
        job = self.db.get_job(self.job['job_id'])
        self.assertFalse(job['audio_ready'])
        self.assertGreater(job['cache_bytes'], 44)

    def test_legacy_queue_deduplicates_across_output_directory_changes(self):
        capture = CapturePayload.create_local(str(self.source))
        stream = capture.media_streams[0]
        old_config = JobConfig.from_dict(self.job['config'])
        legacy = compute_legacy_idempotency_key(capture.tenant_code, capture.course_id,
                  capture.sub_id, stream.stream_id, stream.time_offset, old_config, str(self.source))
        with self.db._get_connection() as conn:
            conn.execute('UPDATE jobs SET idempotency_key = ? WHERE job_id = ?', (legacy, self.job['job_id']))
        existing = self.db.submit_job(capture, stream.stream_id,
                   JobConfig(output_dir=str(self.root / 'other-output'), keep_audio=True))
        self.assertEqual(existing['job_id'], self.job['job_id'])
        self.assertEqual(len(self.db.list_jobs()), 1)

    def test_concurrent_reexports_preserve_notes_and_original_audio(self):
        engine = ASREngine(transcribe_fn=lambda *args: [{'start': 0, 'end': 0.9, 'text': 'synthetic'}])
        JobRunner(self.db, engine)._process_single_job(self.db.get_next_queued_job())
        target = self.root / 'exports'
        target.mkdir()
        original = target / 'audio_16k_mono.wav'
        original.write_bytes(b'user original audio')
        with ThreadPoolExecutor(max_workers=2) as pool:
            exports = list(pool.map(lambda _: self.db.export_job(self.job['job_id'], str(target)), range(2)))
        notes = [Path(result['exported_files']['markdown']) for result in exports]
        self.assertNotEqual(notes[0], notes[1])
        self.assertTrue(all(note.is_file() for note in notes))
        self.assertEqual(original.read_bytes(), b'user original audio')

    def test_heartbeat_continues_while_a_job_is_busy(self):
        initial = self.runner.last_heartbeat
        def busy(job):
            threading.Event().wait(1.2)
            self.assertGreater(self.runner.last_heartbeat, initial)
            self.runner._stop_event.set()
        with patch.object(self.runner, '_process_single_job', side_effect=busy):
            self.runner.run_loop()

    def test_invalid_marker_is_reacquired_and_loading_stage_is_honest(self):
        self.runner._run_prefetch(self.db.peek_next_queued_job())
        job = self.db.get_job(self.job['job_id'])
        (Path(job['output_dir']) / 'acquisition.json').write_text('{invalid', encoding='utf-8')
        engine = ASREngine(transcribe_fn=lambda *args: [{'start': 0, 'end': 0.9, 'text': 'synthetic'}])
        def loading():
            self.assertEqual(self.db.get_job(self.job['job_id'])['progress_info']['stage'], 'loading')
        with patch.object(engine, 'load_model', side_effect=loading), patch(
                'tongji_clipper.worker.runner.extract_pcm_wav', wraps=extract_pcm_wav) as extract:
            JobRunner(self.db, engine)._process_single_job(self.db.get_next_queued_job())
        self.assertEqual(extract.call_count, 1)
        finished = self.db.get_job(self.job['job_id'])
        self.assertEqual(finished['status'], 'completed')
        self.assertIn('last_activity', finished['progress_info'])
