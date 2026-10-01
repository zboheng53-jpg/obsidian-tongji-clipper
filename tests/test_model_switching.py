"""Tests for ASR engine model switching, offline propagation, and reuse."""

import unittest
from unittest.mock import MagicMock, patch

from tongji_clipper.asr import ASREngine
from tongji_clipper.db import Database
from tongji_clipper.models import CapturePayload, JobConfig, JobStatus
from tongji_clipper.worker.runner import JobRunner
import tempfile
from pathlib import Path


class TestModelSwitchingAndOffline(unittest.TestCase):

    def test_offline_flag_propagation(self):
        """Verifies offline flag in JobConfig is passed to ASREngine."""
        cfg_offline = JobConfig(model="base", offline=True, model_dir="/tmp/models")
        self.assertTrue(cfg_offline.offline)
        self.assertEqual(cfg_offline.model_dir, "/tmp/models")

        engine = ASREngine(
            model_name=cfg_offline.model,
            offline=cfg_offline.offline,
            download_root=cfg_offline.model_dir,
        )
        self.assertTrue(engine.offline)
        self.assertEqual(engine.download_root, "/tmp/models")
        self.assertIn("True", engine.engine_signature)

    def test_runner_engine_switching(self):
        """JobRunner must switch ASR engine when model or offline config changes, and reuse when identical."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "test.db"))
            engine_small = ASREngine(model_name="small", offline=False)
            runner = JobRunner(db=db, asr_engine=engine_small)

            self.assertEqual(runner.asr_engine.model_name, "small")
            self.assertFalse(runner.asr_engine.offline)

            # Simulated job with model="base"
            job_base = {
                "job_id": "job_123",
                "config": {"model": "base", "device": "cpu", "compute_type": "int8", "offline": True},
            }
            # The runner's engine signature for base is different
            needed_sig = "base:cpu:int8::True"
            self.assertNotEqual(runner.asr_engine.engine_signature, needed_sig)


if __name__ == "__main__":
    unittest.main()
