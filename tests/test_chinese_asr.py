"""Regressions for Chinese decoding, script conversion and cache identity."""

import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tongji_clipper.asr import ASREngine, simplify_chinese
from tongji_clipper.db import Database
from tongji_clipper.models import CapturePayload, JobConfig


class TestChineseASR(unittest.TestCase):
    def test_decode_hints_and_simplified_export_preserve_raw_and_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.wav"
            with wave.open(str(audio), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(b"\x00\x00" * 16000)
            engine = ASREngine()
            model = Mock()
            model.transcribe.return_value = (iter([
                SimpleNamespace(start=0.1, end=0.9, text="主矢與主矩，力學與 PID 控制。",
                                avg_logprob=-0.2, no_speech_prob=0.01)
            ]), None)
            engine.model = model
            cp = Path(tmp) / "checkpoint.json"
            segments = engine.transcribe_audio_stream(str(audio), 1, hotwords="主矢、主矩", checkpoint_path=str(cp))
            options = model.transcribe.call_args.kwargs
            self.assertIsNone(options.get("initial_prompt"))
            self.assertEqual(options["hotwords"], "主矢、主矩")
            self.assertEqual(segments[0]["text"], "主矢与主矩，力学与 PID 控制。")
            self.assertEqual(segments[0]["raw_text"], "主矢與主矩，力學與 PID 控制。")
            self.assertEqual((segments[0]["start"], segments[0]["end"]), (0.1, 0.9))
            self.assertEqual(segments[0]["avg_logprob"], -0.2)
            saved = json.loads(cp.read_text(encoding="utf-8"))
            self.assertEqual(saved["raw_chunks"][0]["segments"][0]["text"], segments[0]["raw_text"])
            # A changed glossary must not resume a checkpoint decoded with other hints.
            with self.assertRaisesRegex(ValueError, "config mismatch"):
                engine.transcribe_audio_stream(str(audio), 1, hotwords="力偶", checkpoint_path=str(cp))
            self.assertFalse(list(Path(tmp).glob("chunk_*.wav")))

    def test_conversion_is_script_only_and_does_not_rewrite_homophones(self):
        self.assertEqual(simplify_chinese("主使、主劇和力學", "zh"), "主使、主剧和力学")
        self.assertEqual(simplify_chinese("繁體與 English", "en"), "繁體與 English")

    def test_glossary_affects_job_identity_and_old_results_are_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "audio.wav"
            audio.write_bytes(b"synthetic local input")
            capture = CapturePayload.create_local(str(audio))
            db = Database(str(Path(tmp) / "queue.db"))
            cfg = JobConfig(hotwords="主矢、主矩")
            first = db.submit_job(capture, capture.media_streams[0].stream_id, cfg)
            self.assertEqual(first["job_id"], db.submit_job(capture, capture.media_streams[0].stream_id, cfg)["job_id"])
            self.assertNotEqual(first["job_id"], db.submit_job(capture, capture.media_streams[0].stream_id, JobConfig(hotwords="力偶"))["job_id"])
            # Simulate an installation with a pre-upgrade legacy completed result.
            from tongji_clipper.models import compute_legacy_idempotency_key
            identity = dict(tenant_code=capture.tenant_code, course_id=capture.course_id,
                            sub_id=capture.sub_id, stream_id=capture.media_streams[0].stream_id,
                            time_offset=0, config=cfg, local_file_path=str(audio))
            old_config = cfg.to_dict()
            old_config.pop("asr_revision")
            with db._get_connection() as connection:
                connection.execute("UPDATE jobs SET idempotency_key=?, config_json=?, status='completed' WHERE job_id=?",
                                   (compute_legacy_idempotency_key(**identity), json.dumps(old_config), first["job_id"]))
                # Old deterministic ID also predates the new pipeline identity.
                connection.execute("UPDATE jobs SET job_id='job_pre_upgrade' WHERE job_id=?", (first["job_id"],))
            fresh = db.submit_job(capture, capture.media_streams[0].stream_id, cfg)
            self.assertEqual(fresh["status"], "queued")
            self.assertNotEqual(fresh["job_id"], "job_pre_upgrade")
            self.assertEqual(db.get_job("job_pre_upgrade")["status"], "completed")

    def test_glossary_validation(self):
        for value in (None, ["力偶"], "字" * 501):
            with self.assertRaises(ValueError):
                JobConfig.from_dict({"hotwords": value})

    def test_model_timestamp_overrun_cannot_extend_the_recording(self):
        engine = ASREngine(transcribe_fn=lambda *args: [
            {"start": 0.1, "end": 2.0, "text": "真实片段"},
            {"start": 2.0, "end": 3.0, "text": "音频之外的预测"},
        ])
        segments = engine.transcribe_audio_stream("dummy.wav", 1.0, base_time_offset=120)
        self.assertEqual(len(segments), 1)
        self.assertEqual((segments[0]["start"], segments[0]["end"]), (120.1, 121.0))


if __name__ == "__main__":
    unittest.main()
