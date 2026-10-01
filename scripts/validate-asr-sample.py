"""Local-only ASR baseline for the retained 30-second classroom sample."""
import json
import time
from pathlib import Path

from faster_whisper import WhisperModel

root = Path(__file__).resolve().parents[1]
probe = json.loads((root / 'artifacts/probe/latest-probe.json').read_text(encoding='utf-8-sig'))
started = time.perf_counter()
model = WhisperModel(str(root / 'models/faster-whisper-small'), device='cpu', compute_type='int8', local_files_only=True, cpu_threads=8)
loaded = time.perf_counter()
segments, info = model.transcribe(probe['sample_path'], language='zh', beam_size=5, vad_filter=True)
rows = [dict(start=s.start, end=s.end, text=s.text) for s in segments]
finished = time.perf_counter()
result = dict(model='faster-whisper-small', device='cpu', compute_type='int8', language=info.language, duration=info.duration, source_offset_seconds=probe['offset_seconds'], model_load_seconds=loaded-started, inference_seconds=finished-loaded, segments=rows, quality='unreviewed ASR; not a human reference transcript')
destination = root / 'artifacts/probe/asr-baseline.json'
destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False, indent=2))
