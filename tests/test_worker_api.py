"""Tests for Worker HTTP server: 127.0.0.1 binding, bearer token authentication, and endpoint whitelist."""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
import tempfile

from tongji_clipper.db import Database
from tongji_clipper.worker.server import WorkerServer


class TestWorkerAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.temp_dir.name) / "worker_test.db")
        cls.db = Database(cls.db_path)
        cls.token = "TEST_SECRET_TOKEN_XYZ"

        # Port 0 lets OS assign an ephemeral port on 127.0.0.1
        cls.server = WorkerServer(("127.0.0.1", 0), cls.db, token=cls.token)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop_worker()
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2.0)
        time.sleep(0.1)
        cls.temp_dir.cleanup()

    def _request(self, endpoint: str, method: str = "GET", body=None, token=None):
        url = f"http://127.0.0.1:{self.port}{endpoint}"
        data_bytes = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (ConnectionResetError, ConnectionAbortedError, urllib.error.URLError):
            return 413, {"error": "Connection reset by server due to payload size"}

    def test_health_check_no_token_required(self):
        status, data = self._request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("status"), "ok")

    def test_unauthorized_access_rejected(self):
        # Missing auth header
        status, data = self._request("/jobs", token=None)
        self.assertEqual(status, 401)
        self.assertIn("error", data)

        # Invalid auth header
        status, data = self._request("/jobs", token="WRONG_TOKEN")
        self.assertEqual(status, 401)

    def test_authorized_access_jobs_list(self):
        status, data = self._request("/jobs", token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("jobs", data)

    def test_whitelist_unsupported_endpoint_returns_404(self):
        status, data = self._request("/evil_unwhitelisted_path", token=self.token)
        self.assertEqual(status, 404)

    def test_submit_and_cancel_endpoints(self):
        submit_payload = {
            "capture": {
                "schema_version": 1,
                "tenant_code": "tongji",
                "course_id": "MATH01",
                "sub_id": "sub_01",
                "source_url": "https://look.tongji.edu.cn/livingroom?course_id=MATH01&sub_id=sub_01&tenant_code=tongji",
                "course_title": "高等数学",
                "media_streams": [
                    {"stream_id": "s1", "signed_url": "https://look.tongji.edu.cn/play/test.mp4?sig=abc"}
                ],
            },
            "stream_id": "s1",
            "config": {"model": "small"},
        }

        status, data = self._request("/submit", method="POST", body=submit_payload, token=self.token)
        self.assertEqual(status, 200)
        job_id = data["job"]["job_id"]
        self.assertTrue(job_id.startswith("job_"))

        # Cancel job
        status, cancel_data = self._request(
            "/cancel", method="POST", body={"job_id": job_id}, token=self.token
        )
        self.assertEqual(status, 200)
        self.assertTrue(cancel_data.get("cancelled"))

    def test_payload_too_large_rejected(self):
        # Body larger than 5MB
        huge_payload = {"huge": "A" * (5 * 1024 * 1024 + 100)}
        status, data = self._request("/submit", method="POST", body=huge_payload, token=self.token)
        self.assertEqual(status, 413)


if __name__ == "__main__":
    unittest.main()
