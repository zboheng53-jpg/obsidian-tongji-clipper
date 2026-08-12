import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("local_service", ROOT / "local_service.py")
SERVICE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = SERVICE
SPEC.loader.exec_module(SERVICE)

parser = argparse.ArgumentParser(description="Run the real local ASR function on a media sample")
parser.add_argument("media", type=Path)
parser.add_argument("--model-cache", type=Path)
args = parser.parse_args()

settings = SERVICE.Settings.load(Path(__file__).with_name("config.test.json"))
if args.model_cache:
    settings = SERVICE.Settings(
        vault_dir=settings.vault_dir,
        notes_subdir=settings.notes_subdir,
        model=settings.model,
        language=settings.language,
        host=settings.host,
        port=settings.port,
        keep_temporary_media=settings.keep_temporary_media,
        model_cache_dir=args.model_cache.resolve(),
    )
sample = args.media.resolve()
started = time.time()
segments = SERVICE.transcribe_video(
    sample,
    {"course_name": "高级语言程序设计实验", "author": "沈坚"},
    settings,
)
result = {
    "elapsed_seconds": round(time.time() - started, 2),
    "segment_count": len(segments),
    "first_start": segments[0]["start"],
    "last_end": segments[-1]["end"],
    "text": "".join(item["text"] for item in segments),
}
Path(__file__).with_name("real-asr-result.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result, ensure_ascii=True))
