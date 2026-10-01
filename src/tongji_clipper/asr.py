"""ASR engine using faster-whisper with chunking, overlap deduplication, and checkpoints."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

from tongji_clipper.config import get_models_cache_dir

logger = logging.getLogger(__name__)


def clean_text_for_comparison(text: str) -> str:
    """Removes punctuation and whitespace for comparison."""
    return re.sub(r"[\s\.,\?!，。？！；;:：\"'“”‘’（）\(\)\[\]]+", "", text.strip())


def is_overlap_duplicate(
    candidate: Dict[str, Any],
    prev_chunk_segments: List[Dict[str, Any]],
    overlap_start: float,
    overlap_end: float,
) -> bool:
    """
    Deduplication strictly within the chunk overlap window [overlap_start, overlap_end].
    Compares ONLY against segments from previous chunks in the overlap region.
    Does NOT remove legitimate teacher repetitions in normal speech or within same chunk.
    """
    cand_start = candidate["start"]
    cand_end = candidate["end"]

    # If the segment starts after the overlap window, it is entirely new speech in current chunk
    if cand_start >= overlap_end - 0.1:
        return False

    cand_norm = clean_text_for_comparison(candidate["text"])
    if not cand_norm:
        return True  # Skip empty noise

    # Compare only with segments from prior chunk that fall in or near overlap
    for prev in prev_chunk_segments:
        if prev["end"] < overlap_start - 1.0:
            continue

        prev_norm = clean_text_for_comparison(prev["text"])
        overlap_len = max(0.0, min(cand_end, prev["end"]) - max(cand_start, prev["start"]))
        # A substring may contain new words across the boundary; preserve it.
        shorter_duration = min(cand_end - cand_start, prev["end"] - prev["start"])
        if cand_norm == prev_norm and shorter_duration > 0 and overlap_len / shorter_duration >= 0.6 and cand_end <= prev["end"] + 1.0:
            return True

    return False


class ASREngine:
    """
    Wrapper around faster-whisper.
    Supports offline mode (local_files_only=True), chunked transcription (~5min),
    VAD filtering, and structured checkpointing.
    """

    def __init__(
        self,
        model_name: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        download_root: Optional[str] = None,
        offline: bool = False,
        transcribe_fn: Optional[Callable[..., Any]] = None,
    ):
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root or str(get_models_cache_dir())
        self.offline = offline
        self._model = None
        self._mock_transcribe_fn = transcribe_fn

    @property
    def engine_signature(self) -> str:
        """Signature to detect if worker needs to reload engine for a new job."""
        return f"{self.model_name}:{self.device}:{self.compute_type}:{self.download_root}:{self.offline}"

    def load_model(self) -> None:
        """Loads faster-whisper model with strict offline check and user download prompt."""
        if self._mock_transcribe_fn is not None or self._model is not None:
            return

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError(
                "faster-whisper is not installed. Please install it with 'pip install faster-whisper'."
            )

        if self.offline:
            logger.info("Loading model '%s' in strict offline mode (local_files_only=True)", self.model_name)
            try:
                self._model = WhisperModel(
                    self.model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                    download_root=self.download_root,
                    local_files_only=True,
                )
            except Exception as e:
                raise RuntimeError(
                    f"离线模式：本地未找到模型 '{self.model_name}' 缓存，请先联网下载或指定本地模型目录。原因: {e}"
                ) from e
            return

        print(f"[Tongji Clipper] 正在加载/检查 Faster-Whisper 模型 '{self.model_name}' (CPU {self.compute_type})...")
        try:
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
                download_root=self.download_root,
                local_files_only=False,
            )
        except Exception as e:
            raise RuntimeError(f"加载 Faster-Whisper 模型失败: {e}") from e

    def transcribe_chunk(
        self,
        audio_path: str,
        start_sec: float,
        end_sec: float,
        language: str = "zh",
    ) -> List[Dict[str, Any]]:
        """Transcribes a specific audio slice and returns relative segments."""
        if self._mock_transcribe_fn is not None:
            return self._mock_transcribe_fn(audio_path, start_sec, end_sec, language)

        if self._model is None:
            self.load_model()

        import wave
        chunk_wav_path = str(Path(audio_path).parent / f"chunk_{start_sec:.1f}_{end_sec:.1f}.wav")
        try:
            with wave.open(audio_path, "rb") as wf:
                framerate = wf.getframerate()
                nchannels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                start_frame = int(start_sec * framerate)
                end_frame = int(end_sec * framerate)
                total_frames = wf.getnframes()

                start_frame = min(start_frame, total_frames)
                end_frame = min(end_frame, total_frames)
                frames_to_read = max(0, end_frame - start_frame)

                wf.setpos(start_frame)
                chunk_data = wf.readframes(frames_to_read)

            with wave.open(chunk_wav_path, "wb") as out_wf:
                out_wf.setnchannels(nchannels)
                out_wf.setsampwidth(sampwidth)
                out_wf.setframerate(framerate)
                out_wf.writeframes(chunk_data)

            segments, _ = self._model.transcribe(
                chunk_wav_path,
                language=language,
                vad_filter=True,
                beam_size=5,
            )

            results = []
            for s in segments:
                results.append({
                    "start": float(s.start),
                    "end": float(s.end),
                    "text": s.text,
                    "avg_logprob": float(s.avg_logprob),
                    "no_speech_prob": float(s.no_speech_prob),
                })
            return results
        finally:
            p = Path(chunk_wav_path)
            if p.is_file():
                try:
                    p.unlink()
                except OSError:
                    pass

    def transcribe_audio_stream(
        self,
        audio_path: str,
        total_duration: float,
        chunk_duration: float = 300.0,
        chunk_overlap: float = 10.0,
        base_time_offset: float = 0.0,
        language: str = "zh",
        checkpoint_path: Optional[str] = None,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        check_cancelled: Optional[Callable[[], bool]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Processes audio in ~5min chunks with overlap deduplication.
        Records structured checkpoints (next_start, raw_chunks, config_fingerprint).
        Guarantees tail chunk terminates without re-running.
        """
        self.load_model()
        all_segments: List[Dict[str, Any]] = []
        raw_chunks: List[Dict[str, Any]] = []

        audio_hash = hashlib.sha256()
        if Path(audio_path).is_file():
            with open(audio_path, 'rb') as audio_file:
                for block in iter(lambda: audio_file.read(1024 * 1024), b''):
                    audio_hash.update(block)
        config_seed = f"{self.engine_signature}:{language}:{chunk_duration}:{chunk_overlap}:{base_time_offset}:{total_duration}:{audio_hash.hexdigest()}"
        config_fingerprint = hashlib.sha256(config_seed.encode("utf-8")).hexdigest()[:16]

        curr_start = 0.0
        step = chunk_duration - chunk_overlap
        if step <= 0:
            step = chunk_duration

        # 1. Load checkpoint if present
        if checkpoint_path and Path(checkpoint_path).is_file():
            try:
                with open(checkpoint_path, "r", encoding="utf-8") as f:
                    cp = json.load(f)
                if not isinstance(cp, dict):
                    raise ValueError(f"Corrupted checkpoint: expected dict, got {type(cp)}")
                if cp.get("config_fingerprint") != config_fingerprint:
                    raise ValueError(
                        f"Checkpoint config mismatch: {cp.get('config_fingerprint')} != {config_fingerprint}"
                    )
                curr_start = float(cp.get("next_start", 0.0))
                all_segments = list(cp.get("segments", []))
                raw_chunks = list(cp.get("raw_chunks", []))
                logger.info("Resumed from checkpoint: next_start=%.1fs, segments=%d", curr_start, len(all_segments))
            except Exception as e:
                logger.error("Failed loading checkpoint %s: %s", checkpoint_path, e)
                raise ValueError(f"Cannot resume from invalid checkpoint: {e}") from e

        # Calculate estimated total chunks
        remaining_duration = max(0.0, total_duration - curr_start)
        import math
        estimated_chunks = max(1, math.ceil(max(0, total_duration - chunk_duration) / step) + 1)
        chunk_idx = len(raw_chunks)

        while curr_start < total_duration:
            if check_cancelled and check_cancelled():
                raise RuntimeError("ASR transcription cancelled by user")

            curr_end = min(curr_start + chunk_duration, total_duration)
            chunk_idx += 1

            msg = f"分块 {chunk_idx}/{estimated_chunks}: {curr_start/60:.1f}m - {curr_end/60:.1f}m"
            if on_progress:
                on_progress(chunk_idx - 1, estimated_chunks, msg)

            chunk_segments = self.transcribe_chunk(
                audio_path=audio_path,
                start_sec=curr_start,
                end_sec=curr_end,
                language=language,
            )

            # Record raw chunk record
            raw_chunks.append({
                "chunk_index": chunk_idx,
                "start_sec": curr_start,
                "end_sec": curr_end,
                "segments": chunk_segments,
            })

            # Overlap window
            overlap_window_start = curr_start + base_time_offset
            overlap_window_end = (curr_start + chunk_overlap) + base_time_offset if curr_start > 0 else overlap_window_start

            # Segments from previous chunk to compare against
            prev_chunk_segs = [s for s in all_segments if s["start"] < overlap_window_end]

            for s in chunk_segments:
                global_start = curr_start + s["start"] + base_time_offset
                global_end = curr_start + s["end"] + base_time_offset
                global_seg = {
                    "start": round(global_start, 2),
                    "end": round(global_end, 2),
                    "text": s["text"],
                }

                # Deduplicate ONLY across chunk boundary overlap window
                if curr_start > 0 and is_overlap_duplicate(
                    candidate=global_seg,
                    prev_chunk_segments=prev_chunk_segs,
                    overlap_start=overlap_window_start,
                    overlap_end=overlap_window_end,
                ):
                    continue

                all_segments.append(global_seg)

            next_start = total_duration if curr_end >= total_duration else curr_start + step

            # Save checkpoint
            if checkpoint_path:
                tmp_cp = f"{checkpoint_path}.tmp"
                cp_data = {
                    "version": 1,
                    "config_fingerprint": config_fingerprint,
                    "next_start": next_start,
                    "raw_chunks": raw_chunks,
                    "segments": all_segments,
                }
                with open(tmp_cp, "w", encoding="utf-8") as f:
                    json.dump(cp_data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_cp, checkpoint_path)

            if on_progress:
                on_progress(chunk_idx, estimated_chunks, f"已完成分块 {chunk_idx}/{estimated_chunks}")

            # If current chunk reached the exact end of total_duration, terminate cleanly
            if curr_end >= total_duration:
                break

            curr_start = next_start

        # Sort and validate segment timestamps
        all_segments.sort(key=lambda s: s["start"])
        for s in all_segments:
            if s["end"] < s["start"]:
                s["end"] = s["start"]

        return all_segments
