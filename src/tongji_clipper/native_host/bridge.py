"""Native messaging bridge connecting browser extension to persistent localhost worker daemon."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, Optional
import urllib.error
import urllib.request

from tongji_clipper.config import (
    get_app_dir,
    get_worker_info_path,
    load_worker_info,
    remove_worker_info,
)
from tongji_clipper.native_host.protocol import read_message, send_message

class FlushFileHandler(RotatingFileHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

log_file = get_app_dir() / "bridge.log"
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
log_handler = FlushFileHandler(str(log_file), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
log_formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] (bridge) %(message)s")
log_handler.setFormatter(log_formatter)
logger.addHandler(log_handler)


def check_worker_auth(port: int, token: str) -> bool:
    """Verifies that the worker on port accepts token without error."""
    if not token:
        return False
    url = f"http://127.0.0.1:{port}/jobs?limit=1"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 401:
            logger.warning("Worker on port %d rejected auth token (401)", port)
        return False
    except Exception:
        return False


def ping_worker(
    port: int,
    token: Optional[str] = None,
    expected_pid: Optional[int] = None,
) -> bool:
    """Checks worker health on localhost port, optionally validating PID and token."""
    url = f"http://127.0.0.1:{port}/health"
    try:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
            if expected_pid is not None and data.get("pid") is not None:
                if data["pid"] != expected_pid:
                    logger.warning(
                        "Worker on port %d has PID %s, expected PID %s",
                        port,
                        data.get("pid"),
                        expected_pid,
                    )
                    return False
    except Exception:
        return False

    if token:
        return check_worker_auth(port, token)
    return True


def ensure_worker_running(max_wait_sec: float = 6.0) -> Dict[str, Any]:
    """Ensures independent background worker is running. Auto-spawns if inactive."""
    info = load_worker_info()
    if info:
        port = int(info.get("port", 0))
        token = info.get("token", "")
        pid = info.get("pid")
        if ping_worker(port, token=token, expected_pid=pid):
            return info
        logger.warning(
            "Worker info in %s is stale or rejected by worker on port %d. Cleaning up...",
            get_worker_info_path(),
            port,
        )
        remove_worker_info()

    logger.info("Worker not detected. Spawning new detached worker process...")

    # Clear any stale worker info before spawning to avoid reading outdated tokens in poll loop
    remove_worker_info()

    # Spawn worker detached on Windows
    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS = 0x00000008, CREATE_NEW_PROCESS_GROUP = 0x00000200
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    cmd = [sys.executable, "-m", "tongji_clipper.worker.server"]
    subprocess.Popen(
        cmd,
        creationflags=creationflags,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    # Poll until ready
    start_time = time.time()
    while time.time() - start_time < max_wait_sec:
        time.sleep(0.3)
        info = load_worker_info()
        if info:
            port = int(info.get("port", 0))
            token = info.get("token", "")
            pid = info.get("pid")
            if ping_worker(port, token=token, expected_pid=pid):
                logger.info("Worker spawned and ready on port %s (PID %s)", port, pid)
                return info

    raise RuntimeError("Failed to auto-start worker daemon within timeout")


def send_worker_request(
    endpoint: str,
    method: str = "GET",
    body: Optional[Dict[str, Any]] = None,
    _retry_auth: bool = True,
) -> Dict[str, Any]:
    worker_info = ensure_worker_running()
    port = int(worker_info["port"])
    token = worker_info["token"]

    url = f"http://127.0.0.1:{port}{endpoint}"
    data_bytes = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if body is not None:
        data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            resp_body = resp.read().decode("utf-8")
            return json.loads(resp_body)
    except urllib.error.HTTPError as e:
        if e.code == 401 and _retry_auth:
            logger.warning(
                "Worker returned 401 Unauthorized for token. Attempting automatic recovery..."
            )
            # Check if token on disk was updated by another worker instance
            refreshed = load_worker_info()
            if refreshed and refreshed.get("token") != token:
                logger.info("Detected new token on disk, retrying worker request...")
                return send_worker_request(endpoint, method=method, body=body, _retry_auth=False)

            # Otherwise remove stale worker.json and re-ensure worker
            remove_worker_info()
            try:
                ensure_worker_running()
                return send_worker_request(endpoint, method=method, body=body, _retry_auth=False)
            except Exception as rec_err:
                logger.error("Failed to auto-recover from 401 worker auth error: %s", rec_err)

        err_msg = e.read().decode("utf-8") if e.fp else str(e)
        logger.error("HTTP error from worker: %s", err_msg)
        return {"error": f"Worker error ({e.code}): {err_msg}"}
    except Exception as e:
        logger.error("Error communicating with worker: %s", e)
        return {"error": f"Communication error: {e}"}


def handle_client_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Routes action to worker API endpoints."""
    if not isinstance(msg, dict):
        return {"error": "Message must be a JSON object"}

    action = msg.get("action", "")

    if action == "ping":
        worker_info = ensure_worker_running()
        return {"status": "ok", "worker_pid": worker_info.get("pid"), "port": worker_info.get("port")}

    if action == "client_log":
        logger.info("BATCH_LOG: %s", msg.get("message"))
        return {"status": "ok"}

    if action == "submit":
        payload = msg.get("payload", {})
        stream_id = msg.get("stream_id", "")
        config = msg.get("config", {})
        return send_worker_request(
            "/submit",
            method="POST",
            body={"capture": payload, "stream_id": stream_id, "config": config},
        )

    if action == "list_jobs":
        params = []
        if "limit" in msg:
            params.append(f"limit={msg['limit']}")
        if "offset" in msg:
            params.append(f"offset={msg['offset']}")
        qs = f"?{'&'.join(params)}" if params else ""
        return send_worker_request(f"/jobs{qs}", method="GET")

    if action == "status":
        job_id = msg.get("job_id", "")
        return send_worker_request(f"/jobs/{job_id}", method="GET")

    if action == "system_info":
        return send_worker_request("/system_info", method="GET")

    if action == "validate_config":
        return send_worker_request(
            "/validate_config",
            method="POST",
            body={"config": msg.get("config", {})},
        )

    if action == "open_output":
        return send_worker_request(
            "/open_output",
            method="POST",
            body={"job_id": msg.get("job_id"), "kind": msg.get("kind", "folder")},
        )

    if action == "export":
        return send_worker_request(
            "/export",
            method="POST",
            body={
                "job_id": msg.get("job_id"),
                "output_dir": msg.get("output_dir"),
                "keep_audio": bool(msg.get("keep_audio", False)),
            },
        )

    if action == "cleanup_cache":
        return send_worker_request(
            "/cleanup_cache",
            method="POST",
            body={"job_id": msg.get("job_id")},
        )

    if action == "preview":
        return send_worker_request(
            "/preview",
            method="POST",
            body={"job_id": msg.get("job_id"), "max_lines": int(msg.get("max_lines", 50))},
        )

    if action == "cancel":
        job_id = msg.get("job_id", "")
        return send_worker_request("/cancel", method="POST", body={"job_id": job_id})

    if action == "pause":
        job_id = msg.get("job_id", "")
        return send_worker_request("/pause", method="POST", body={"job_id": job_id})

    if action == "resume":
        job_id = msg.get("job_id", "")
        return send_worker_request("/resume", method="POST", body={"job_id": job_id})

    if action == "retry":
        job_id = msg.get("job_id", "")
        return send_worker_request("/retry", method="POST", body={"job_id": job_id})

    if action == "refresh_signature":
        job_id = msg.get("job_id", "")
        signed_url = msg.get("signed_url", "")
        return send_worker_request(
            "/refresh_signature",
            method="POST",
            body={"job_id": job_id, "signed_url": signed_url},
        )

    return {"error": f"Unsupported action: '{action}'"}


def main() -> None:
    logger.info("Native messaging bridge started.")
    while True:
        try:
            msg = read_message()
        except Exception as e:
            logger.exception("Failed reading message from pipe: %s", e)
            break

        if msg is None:
            logger.info("Native messaging client closed pipe. Exiting bridge.")
            break

        logger.info("Received message action: %s", msg.get("action") if isinstance(msg, dict) else msg)
        try:
            response = handle_client_message(msg)
            if not isinstance(response, dict):
                response = {"status": "ok", "result": response}
            if isinstance(msg, dict) and "request_id" in msg:
                response["request_id"] = msg["request_id"]
            send_message(response)
            logger.info("Sent response for action: %s (req_id: %s)", msg.get("action"), msg.get("request_id"))
        except Exception as e:
            logger.exception("Bridge encountered error handling message: %s", e)
            try:
                err_resp: Dict[str, Any] = {"error": f"Bridge error: {e}"}
                if isinstance(msg, dict) and "request_id" in msg:
                    err_resp["request_id"] = msg["request_id"]
                send_message(err_resp)
            except Exception:
                pass


if __name__ == "__main__":
    main()
