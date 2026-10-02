"""Actual Windows GUI native-host pipes and non-starting idle requests."""

import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from tongji_clipper.native_host.bridge import send_worker_request


class TestBackgroundRuntime(unittest.TestCase):
    def test_shutdown_refuses_active_work_and_stops_after_pause(self):
        from tongji_clipper.db import Database
        from tongji_clipper.models import CapturePayload, JobConfig, JobStatus
        from tongji_clipper.worker.server import WorkerServer
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "queue.db"))
            audio = Path(tmp) / "local.wav"
            audio.write_bytes(b"synthetic local input")
            capture = CapturePayload.create_local(str(audio))
            job = db.submit_job(capture, capture.media_streams[0].stream_id, JobConfig())
            server = WorkerServer(("127.0.0.1", 0), db, token="synthetic-stop-test")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_address[1]}/shutdown"
            request = urllib.request.Request(url, data=b'{"only_if_idle":true}',
                      headers={"Authorization": "Bearer synthetic-stop-test", "Content-Type": "application/json"}, method="POST")
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(caught.exception.code, 409)
                caught.exception.close()
                self.assertEqual(db.get_job(job["job_id"])["status"], "queued")
                db.update_job_status(job["job_id"], JobStatus.PAUSED)
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(json.load(response)["status"], "shutting_down")
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            finally:
                server.shutdown()
                server.stop_worker()
                server.server_close()
                thread.join(timeout=3)

    def test_stopped_service_is_not_restarted_by_poll_or_shutdown(self):
        with patch("tongji_clipper.native_host.bridge.load_worker_info", return_value=None), \
             patch("tongji_clipper.native_host.bridge.ensure_worker_running") as start:
            self.assertEqual(send_worker_request("/jobs", auto_start=False)["status"], "stopped")
            self.assertEqual(send_worker_request("/shutdown", "POST", auto_start=False)["status"], "stopped")
            start.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows GUI launcher integration")
    def test_gui_host_roundtrip_multiple_messages_and_eof(self):
        host = Path(__file__).resolve().parents[1] / ".venv/Scripts/tongji-bridge.exe"
        if not host.is_file():
            self.skipTest("Install project in .venv to test GUI launcher")
        messages = [{"action": "client_log", "message": "测试\n中文管道", "request_id": str(i)} for i in range(2)]
        payload = b"".join(struct.pack("<I", len(data)) + data for data in
                           (json.dumps(msg, ensure_ascii=False).encode() for msg in messages))
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([str(host)], input=payload, capture_output=True, timeout=15,
                                    env=dict(os.environ, TONGJI_CLIPPER_HOME=tmp))
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            data = result.stdout
            for message in messages:
                length = struct.unpack("<I", data[:4])[0]
                reply = json.loads(data[4:4 + length])
                self.assertEqual(reply, {"status": "ok", "request_id": message["request_id"]})
                data = data[4 + length:]
            self.assertEqual(data, b"")
            self.assertFalse((Path(tmp) / "worker.json").exists())


if __name__ == "__main__":
    unittest.main()
