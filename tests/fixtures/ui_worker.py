"""Isolated worker with synthetic completed jobs for browser integration tests."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import wave


def main():
    with tempfile.TemporaryDirectory(prefix="clipper-ui-") as folder:
        root = Path(folder)
        os.environ["TONGJI_CLIPPER_HOME"] = str(root / "runtime")
        from tongji_clipper.asr import ASREngine
        from tongji_clipper.db import Database
        from tongji_clipper.models import CapturePayload, JobConfig
        from tongji_clipper.worker.runner import JobRunner
        from tongji_clipper.worker.server import WorkerServer

        source = root / "fixture.wav"
        with wave.open(str(source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 16000)
        db = Database(str(root / "queue.db"))
        capture = CapturePayload.create_local(str(source), title="合成接口验收课程")
        job = db.submit_job(capture, capture.media_streams[0].stream_id, JobConfig(output_dir=str(root / "output"), keep_audio=True))
        engine = ASREngine(transcribe_fn=lambda *args: [{"start": 0.0, "end": 0.8, "text": "这是接口验收的合成文本。", "avg_logprob": -1.2, "no_speech_prob": 0.1}])
        JobRunner(db, engine)._process_single_job(db.get_next_queued_job())
        assert db.get_job(job["job_id"])["status"] == "completed"
        server = WorkerServer(("127.0.0.1", 0), db, token="synthetic-ui-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_address[1]}", "job_id": job["job_id"], "export_dir": str(root / "reexport")}), flush=True)
        try:
            sys.stdin.readline()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    main()
