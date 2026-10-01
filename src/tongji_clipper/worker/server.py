"""Localhost HTTP server for Tongji Clipper background worker daemon."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import sys
import socket
import threading
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from tongji_clipper.config import (
    generate_auth_token,
    load_worker_info,
    remove_worker_info,
    save_worker_info,
)
from tongji_clipper.db import Database
from tongji_clipper.models import CapturePayload, JobConfig
from tongji_clipper.worker.runner import JobRunner

logger = logging.getLogger(__name__)

MAX_BODY_SIZE = 5 * 1024 * 1024  # 5 MiB protection


class WorkerRequestHandler(BaseHTTPRequestHandler):
    server: WorkerServer  # Type hint

    def setup(self) -> None:
        super().setup()
        self.timeout = 10.0  # 10s socket timeout

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)

    def _send_json(self, status_code: int, data: Dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        auth_header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        if auth_header != expected:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "Unauthorized: invalid or missing bearer token"},
            )
            return False
        return True

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0:
                return {}
            if content_length > MAX_BODY_SIZE:
                self.close_connection = True
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Payload exceeds 5MB limit"})
                return None
            raw_body = self.rfile.read(content_length)
            return json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Invalid JSON body: {e}"})
            return None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "pid": os.getpid()})
            return

        if not self._check_auth():
            return

        if path == "/jobs":
            jobs = self.server.db.list_jobs()
            self._send_json(HTTPStatus.OK, {"jobs": jobs})
            return

        if path.startswith("/jobs/"):
            job_id = path[len("/jobs/"):]
            job = self.server.db.get_job(job_id, include_signature=False)
            if not job:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Job {job_id} not found"})
                return
            self._send_json(HTTPStatus.OK, {"job": job})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Endpoint not found: {path}"})

    def do_POST(self) -> None:
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        body = self._read_json_body()
        if body is None:
            return

        if path == "/submit":
            try:
                capture_data = body.get("capture")
                if not capture_data:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'capture' in body"})
                    return
                capture = CapturePayload.from_dict(capture_data)
                capture.validate()

                stream_id = body.get("stream_id", "")
                cfg_data = body.get("config", {})
                config = JobConfig.from_dict(cfg_data)

                job = self.server.db.submit_job(capture, stream_id=stream_id, config=config)
                self._send_json(HTTPStatus.OK, {"status": "ok", "job": job})
                return
            except Exception as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Failed submitting job: {e}"})
                return

        if path == "/cancel":
            job_id = body.get("job_id")
            if not job_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'job_id'"})
                return
            ok = self.server.runner.cancel_job(job_id)
            self._send_json(HTTPStatus.OK, {"status": "ok", "cancelled": ok, "job_id": job_id})
            return

        if path == "/refresh_signature":
            job_id = body.get("job_id")
            signed_url = body.get("signed_url")
            if not job_id or not signed_url:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'job_id' or 'signed_url'"})
                return
            try:
                job = self.server.db.refresh_signature(job_id, signed_url)
                if not job:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Job {job_id} not found"})
                    return
                self._send_json(HTTPStatus.OK, {"status": "ok", "job": job})
                return
            except Exception as e:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"Failed refreshing signature: {e}"})
                return

        if path == "/shutdown":
            self._send_json(HTTPStatus.OK, {"status": "shutting_down"})
            threading.Thread(target=self.server.shutdown).start()
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Endpoint not found: {path}"})


class WorkerServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(
        self,
        server_address: tuple[str, int],
        db: Database,
        token: str,
        asr_engine: Optional[Any] = None,
    ):
        super().__init__(server_address, WorkerRequestHandler)
        self.db = db
        self.token = token
        self.runner = JobRunner(db, asr_engine=asr_engine)
        self._runner_thread = threading.Thread(target=self.runner.run_loop, daemon=True)

    def start_worker(self) -> None:
        self._runner_thread.start()

    def stop_worker(self) -> None:
        self.runner.stop()
        if self._runner_thread.is_alive():
            self._runner_thread.join(timeout=2.0)
        info = load_worker_info()
        if info and info.get('pid') == os.getpid() and info.get('port') == self.server_address[1]:
            remove_worker_info()


def run_server(
    port: int = 48721,
    db_path: Optional[str] = None,
    token: Optional[str] = None,
    asr_engine: Optional[Any] = None,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] (worker) %(message)s",
    )
    bind_host = "127.0.0.1"  # STRICTLY 127.0.0.1
    auth_token = token or generate_auth_token()
    db = Database(db_path)

    # Bind socket FIRST before DB recovery or thread start
    try:
        server = WorkerServer((bind_host, port), db, token=auth_token, asr_engine=asr_engine)
    except OSError as e:
        logger.error("Failed to bind worker to %s:%d: %s. Another worker instance may be active.", bind_host, port, e)
        sys.exit(1)

    actual_port = server.server_address[1]
    save_worker_info(port=actual_port, pid=os.getpid(), token=auth_token)

    logger.info("Worker bound and started on http://%s:%d (PID %d)", bind_host, actual_port, os.getpid())
    server.start_worker()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user")
    finally:
        server.stop_worker()
        server.server_close()
        logger.info("Worker terminated cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tongji Clipper Background Worker Daemon")
    parser.add_argument("--port", type=int, default=48721, help="Port to bind (127.0.0.1 only)")
    parser.add_argument("--token", type=str, default=None, help="Auth token")
    args = parser.parse_args()
    run_server(port=args.port, token=args.token)


if __name__ == "__main__":
    main()
