"""End-to-end integration test verifying complete pipeline with mock ASR engine."""

import json
import tempfile
import unittest
from pathlib import Path

from tongji_clipper.asr import ASREngine
from tongji_clipper.db import Database
from tongji_clipper.models import CapturePayload, JobConfig, JobStatus
from tongji_clipper.worker.runner import JobRunner


class TestEndToEndPipeline(unittest.TestCase):

    def test_full_pipeline_with_local_audio(self):
        # 1. Create a dummy test WAV
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            wav_path = temp_root / "sample_input.wav"
            out_dir = temp_root / "final_output"

            # Generate minimal valid WAV file with standard wave module
            import wave
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                # 1 second of silence (16000 samples of 16-bit 0)
                wf.writeframes(b"\x00\x00" * 16000)

            # 2. Mock ASR engine returning predictable transcript
            def mock_transcribe(audio_path, start_sec, end_sec, language):
                return [
                    {"start": 0.1, "end": 0.8, "text": "欢迎大家选修本课程。"}
                ]

            mock_engine = ASREngine(transcribe_fn=mock_transcribe)

            # 3. Setup DB and Runner
            db_path = str(temp_root / "pipeline_test.db")
            db = Database(db_path)
            runner = JobRunner(db=db, asr_engine=mock_engine)

            # 4. Submit local audio job
            capture = CapturePayload.create_local(
                file_path=str(wav_path),
                title="同济大学演示课",
                lecturer="李教授",
            )
            config = JobConfig(
                model="small",
                output_dir=str(out_dir),
                keep_audio=False,
            )

            stream_id = capture.media_streams[0].stream_id
            job = db.submit_job(capture, stream_id=stream_id, config=config)
            self.assertEqual(job["status"], JobStatus.QUEUED.value)
            job_id = job["job_id"]

            # 5. Process job in runner
            queued_job = db.get_next_queued_job()
            self.assertIsNotNone(queued_job)
            runner._process_single_job(queued_job)

            # 6. Verify job status
            completed_job = db.get_job(job_id)
            self.assertEqual(completed_job["status"], JobStatus.COMPLETED.value)
            self.assertIsNotNone(completed_job["output_dir"])

            job_out_dir = Path(completed_job["output_dir"])
            self.assertTrue(job_out_dir.exists())

            # 7. Check exported files
            md_files = list(job_out_dir.glob("*.md"))
            srt_files = list(job_out_dir.glob("*.srt"))
            json_files = list(job_out_dir.glob("*.segments.json"))

            self.assertEqual(len(md_files), 1)
            self.assertEqual(len(srt_files), 1)
            self.assertEqual(len(json_files), 1)

            # Check Markdown content
            md_content = md_files[0].read_text(encoding="utf-8")
            self.assertIn('title: "同济大学演示课"', md_content)
            self.assertIn("[[李教授]]", md_content)
            self.assertIn("欢迎大家选修本课程。", md_content)

            # Check SRT content
            srt_content = srt_files[0].read_text(encoding="utf-8")
            self.assertIn("00:00:00,100 --> 00:00:00,800", srt_content)
            self.assertIn("欢迎大家选修本课程。", srt_content)

            # Check Segments JSON
            segments = json.loads(json_files[0].read_text(encoding="utf-8"))
            self.assertEqual(len(segments), 1)
            self.assertEqual(segments[0]["text"], "欢迎大家选修本课程。")

            # Check temp audio cleanup (keep_audio is False)
            temp_wav = job_out_dir / "audio_16k_mono.wav"
            self.assertFalse(temp_wav.exists(), "Temporary audio should be cleaned up by default")


if __name__ == "__main__":
    unittest.main()
