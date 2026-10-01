"""Tests for chunk overlap deduplication vs genuine speaker repetition, global timeline mapping, and checkpoints."""

import json
import tempfile
import unittest
from pathlib import Path

from tongji_clipper.asr import ASREngine, is_overlap_duplicate


class TestDeduplicationAndChunking(unittest.TestCase):

    def test_overlap_deduplication_removes_boundary_duplicate(self):
        """Verifies duplicate segment produced in the chunk overlap window is removed."""
        prev_chunk = [
            {"start": 290.0, "end": 298.5, "text": "下面我们来看奈奎斯特采样定理。"},
        ]

        # Chunk 2 starts at 290.0, overlap window is [290.0, 300.0]
        # Candidate segment in chunk 2 starts at 290.5 and says the same text
        candidate = {"start": 290.5, "end": 298.8, "text": "下面我们来看奈奎斯特采样定理"}

        is_dup = is_overlap_duplicate(
            candidate=candidate,
            prev_chunk_segments=prev_chunk,
            overlap_start=290.0,
            overlap_end=300.0,
        )
        self.assertTrue(is_dup, "Boundary duplicate in overlap window should be detected as duplicate")

    def test_preserves_authentic_speaker_repetition_in_continuous_speech(self):
        """CRITICAL: Teacher repeating words for emphasis outside overlap or sequentially MUST be preserved!"""
        prev_chunk = [
            {"start": 280.0, "end": 288.0, "text": "注意看这个公式。"},
        ]

        # Candidate starts after overlap window [290, 300]
        repetition = {"start": 305.0, "end": 308.0, "text": "注意看这个公式。"}

        is_dup = is_overlap_duplicate(
            candidate=repetition,
            prev_chunk_segments=prev_chunk,
            overlap_start=290.0,
            overlap_end=300.0,
        )
        self.assertFalse(is_dup, "Authentic speaker repetition outside overlap window MUST NOT be discarded")

    def test_chunk_loop_global_time_mapping_and_checkpoints(self):
        """Tests that chunk loop correctly shifts segment timestamps and records structured checkpoint."""
        def mock_transcribe(audio_path, start_sec, end_sec, language):
            return [
                {"start": 2.0, "end": 8.0, "text": f"讲话在块 {start_sec}-{end_sec}"}
            ]

        with tempfile.TemporaryDirectory() as tmp:
            cp_path = Path(tmp) / "checkpoint.json"
            engine = ASREngine(transcribe_fn=mock_transcribe)
            progress = []

            # Total duration 700s, chunk duration 300s, overlap 10s
            segments = engine.transcribe_audio_stream(
                audio_path="dummy.wav",
                total_duration=700.0,
                chunk_duration=300.0,
                chunk_overlap=10.0,
                base_time_offset=15.0,
                checkpoint_path=str(cp_path),
                on_progress=lambda done, total, msg: progress.append((done, total)),
            )

            self.assertEqual(progress, [(0, 3), (1, 3), (1, 3), (2, 3), (2, 3), (3, 3)])

            self.assertEqual(len(segments), 3)
            self.assertAlmostEqual(segments[0]["start"], 17.0, places=1)
            self.assertAlmostEqual(segments[1]["start"], 307.0, places=1)
            self.assertAlmostEqual(segments[2]["start"], 597.0, places=1)

            # Verify structured checkpoint file
            self.assertTrue(cp_path.is_file())
            with open(cp_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertIn("next_start", saved)
            self.assertIn("raw_chunks", saved)
            self.assertIn("config_fingerprint", saved)
            self.assertEqual(len(saved["raw_chunks"]), 3)

    def test_silent_chunk_recovery(self):
        """A silent chunk (0 segments) advances next_start and resumes cleanly without re-running."""
        # Chunk 1 returns segments, Chunk 2 is silent, Chunk 3 returns segments
        def mock_transcribe(audio_path, start_sec, end_sec, language):
            if start_sec == 290.0:
                return []  # Silent chunk
            return [{"start": 1.0, "end": 5.0, "text": f"音频 {start_sec}"}]

        with tempfile.TemporaryDirectory() as tmp:
            cp_path = Path(tmp) / "checkpoint.json"
            engine = ASREngine(transcribe_fn=mock_transcribe)

            segments = engine.transcribe_audio_stream(
                audio_path="dummy.wav",
                total_duration=700.0,
                chunk_duration=300.0,
                chunk_overlap=10.0,
                checkpoint_path=str(cp_path),
            )

            # Chunk 1 had 1 seg, Chunk 2 had 0 segs, Chunk 3 had 1 seg -> total 2 segments
            self.assertEqual(len(segments), 2)

            with open(cp_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            # All 3 chunks recorded in raw_chunks
            self.assertEqual(len(saved["raw_chunks"]), 3)
            self.assertEqual(len(saved["raw_chunks"][1]["segments"]), 0)

    def test_tail_chunk_no_duplicate_processing(self):
        """Total duration 450s with chunk 300s terminates after chunk 2 [290, 450] without trailing slice."""
        chunks_called = []
        def mock_transcribe(audio_path, start_sec, end_sec, language):
            chunks_called.append((start_sec, end_sec))
            return [{"start": 1.0, "end": 3.0, "text": "话语"}]

        engine = ASREngine(transcribe_fn=mock_transcribe)
        engine.transcribe_audio_stream(
            audio_path="dummy.wav",
            total_duration=450.0,
            chunk_duration=300.0,
            chunk_overlap=10.0,
        )

        # Expected exactly 2 chunks: [0, 300], [290, 450]
        self.assertEqual(len(chunks_called), 2)
        self.assertEqual(chunks_called[0], (0.0, 300.0))
        self.assertEqual(chunks_called[1], (290.0, 450.0))

    def test_corrupt_checkpoint_raises_error(self):
        """Corrupted checkpoint raises ValueError and is not silently ignored."""
        with tempfile.TemporaryDirectory() as tmp:
            cp_path = Path(tmp) / "checkpoint.json"
            cp_path.write_text("invalid json content!", encoding="utf-8")

            engine = ASREngine(transcribe_fn=lambda *args: [])
            with self.assertRaises(ValueError):
                engine.transcribe_audio_stream(
                    audio_path="dummy.wav",
                    total_duration=100.0,
                    checkpoint_path=str(cp_path),
                )


if __name__ == "__main__":
    unittest.main()
