import json
from pathlib import Path
import tempfile
import unittest
import wave

from tongji_clipper.asr import ASREngine, is_overlap_duplicate
from tongji_clipper.db import Database
from tongji_clipper.exporter import export_job_results
from tongji_clipper.models import CapturePayload, JobConfig, JobStatus
from tongji_clipper.worker.runner import JobRunner


class FinalRegressions(unittest.TestCase):
    def test_completed_checkpoint_does_not_retranscribe_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            cp = str(Path(folder) / 'checkpoint.json')
            calls = []
            engine = ASREngine(transcribe_fn=lambda path, start, end, language: calls.append(start) or [])
            kwargs = dict(audio_path='dummy.wav', total_duration=590, checkpoint_path=cp)
            engine.transcribe_audio_stream(**kwargs)
            self.assertEqual(calls, [0, 290])
            engine.transcribe_audio_stream(**kwargs)
            self.assertEqual(calls, [0, 290])

    def test_changed_audio_rejects_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / 'audio.wav'
            audio.write_bytes(b'original')
            engine = ASREngine(transcribe_fn=lambda *args: [])
            kwargs = dict(audio_path=str(audio), total_duration=20, checkpoint_path=str(Path(folder) / 'checkpoint.json'))
            engine.transcribe_audio_stream(**kwargs)
            audio.write_bytes(b'different')
            with self.assertRaises(ValueError):
                engine.transcribe_audio_stream(**kwargs)

    def test_repetition_without_time_overlap_is_preserved(self):
        self.assertFalse(is_overlap_duplicate({'start':299, 'end':300, 'text':'注意'}, [{'start':297, 'end':298, 'text':'注意'}], 290, 300))

    def test_boundary_substring_with_new_words_is_preserved(self):
        self.assertFalse(is_overlap_duplicate({'start':295, 'end':305, 'text':'注意这个公式下一步'}, [{'start':295, 'end':299, 'text':'注意这个公式'}], 290, 300))

    def test_existing_subtitle_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            existing = out / 'c_s.srt'
            existing.write_text('user edit', encoding='utf-8')
            paths = export_job_results({'course_id':'c','sub_id':'s'}, [{'start':0, 'end':1, 'text':'test'}], out, {})
            self.assertEqual(existing.read_text(encoding='utf-8'), 'user edit')
            self.assertIn('_rev1.md', paths['markdown'])

    def test_silent_transcript_exports_successfully(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = export_job_results({'course_id':'c','sub_id':'s'}, [], Path(folder), {})
            self.assertEqual(json.loads(Path(paths['segments']).read_text()), [])

    def test_invalid_chunk_parameters_are_rejected(self):
        for duration, overlap in [(0,0), (300,300), (float('nan'),0), (300,-1)]:
            with self.assertRaises(ValueError):
                JobConfig(chunk_duration=duration, chunk_overlap=overlap)

    def test_chinese_local_filename_and_offset_are_valid(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / '课程.wav'
            audio.write_bytes(b'input')
            capture = CapturePayload.create_local(str(audio), time_offset=120)
            capture.validate()
            self.assertEqual(capture.media_streams[0].time_offset, 120)

    def test_job_claim_and_cancel_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / 'test.wav'
            audio.write_bytes(b'input')
            db = Database(str(Path(folder) / 'queue.db'))
            cap = CapturePayload.create_local(str(audio))
            submitted = db.submit_job(cap, cap.media_streams[0].stream_id, JobConfig())
            claim = db.get_next_queued_job()
            self.assertEqual(claim['status'], 'running')
            self.assertIsNone(db.get_next_queued_job())
            db.cancel_job(submitted['job_id'])
            db.update_job_status(submitted['job_id'], JobStatus.COMPLETED)
            self.assertEqual(db.get_job(submitted['job_id'])['status'], 'cancelled')

    def test_retry_reuses_verified_audio_after_source_disappears(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audio = root / 'source.wav'
            with wave.open(str(audio), 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes(b'\0\0' * 16000)
            cap = CapturePayload.create_local(str(audio))
            db = Database(str(root / 'queue.db'))
            job = db.submit_job(cap, cap.media_streams[0].stream_id, JobConfig(output_dir=str(root / 'output')))
            engine = ASREngine(transcribe_fn=lambda *args: (_ for _ in ()).throw(RuntimeError('interrupted')))
            runner = JobRunner(db, engine)
            runner._process_single_job(db.get_next_queued_job())
            self.assertEqual(db.get_job(job['job_id'])['status'], 'failed')
            audio.unlink()
            engine._mock_transcribe_fn = lambda *args: [{'start':0, 'end':0.8, 'text':'完成'}]
            db.update_job_status(job['job_id'], JobStatus.QUEUED)
            runner._process_single_job(db.get_next_queued_job())
            self.assertEqual(db.get_job(job['job_id'])['status'], 'completed')
