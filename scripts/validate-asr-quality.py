"""Compare decoding on a user-selected local sample without changing old outputs."""

import argparse
import json
from pathlib import Path
import time
import wave

from tongji_clipper.asr import ASREngine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--baseline-model", default=None)
    parser.add_argument("--cpu-threads", type=int, default=4)
    parser.add_argument("--hotwords", default="")
    parser.add_argument("--output", default=".runtime/asr-quality-check.json")
    args = parser.parse_args()
    audio = Path(args.audio).resolve()
    with wave.open(str(audio), "rb") as wav:
        duration = wav.getnframes() / wav.getframerate()
    baseline_engine = ASREngine(model_name=args.baseline_model or args.model, offline=True, cpu_threads=args.cpu_threads)
    baseline_engine.load_model()
    started = time.perf_counter()
    baseline, _ = baseline_engine.model.transcribe(str(audio), language="zh", vad_filter=True, beam_size=5)
    baseline_rows = [dict(start=s.start, end=s.end, text=s.text) for s in baseline]
    baseline_seconds = time.perf_counter() - started
    baseline_engine.model = None
    engine = ASREngine(model_name=args.model, offline=True, cpu_threads=args.cpu_threads)
    started = time.perf_counter()
    engine.load_model()
    model_load_seconds = time.perf_counter() - started
    started = time.perf_counter()
    improved = engine.transcribe_audio_stream(str(audio), duration, hotwords=args.hotwords)
    improved_seconds = time.perf_counter() - started
    result = dict(duration=duration, hotwords=args.hotwords, baseline=baseline_rows,
                  baseline_model=args.baseline_model or args.model, improved_model=args.model,
                  cpu_threads=args.cpu_threads, model_load_seconds=model_load_seconds,
                  improved=improved, baseline_seconds=baseline_seconds,
                  improved_seconds=improved_seconds,
                  quality="Unreviewed ASR comparison; no human reference or accuracy claim")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
