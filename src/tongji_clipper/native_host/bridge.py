"""Native messaging bridge connecting browser extension to persistent localhost worker daemon."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, Optional
import urllib.error
import urllib.request

from tongji_clipper.config import get_app_dir, load_worker_info
from tongji_clipper.native_host.protocol import read_message, send_message

# Bridge logs strictly to stderr or log file, NEVER stdout!
log_file = get_app_dir() / "bridge.log"
logging.basicConfig(
    filename=str(log_file),
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] (bridge) %(message)s",
)
logger = logging.getLogger(__name__)


def ping_worker(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_worker_running(max_wait_sec: float = 6.0) -> Dict[str, Any]:
    """Ensures independent background worker is running. Auto-spawns if inactive."""
    info = load_worker_info()
    if info and ping_worker(int(info["port"])):
        return info

    logger.info("Worker not detected. Spawning new detached worker process...")

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
        time.sleep(0.4)
        info = load_worker_info()
        if info and ping_worker(int(info["port"])):
            logger.info("Worker spawned and ready on port %s", info["port"])
            return info

    raise RuntimeError("Failed to auto-start worker daemon within timeout")


def send_worker_request(
    endpoint: str,
    method: str = "GET",
    body: Optional[Dict[str, Any]] = None,
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
        err_msg = e.read().decode("utf-8") if e.fp else str(e)
        logger.error("HTTP error from worker: %s", err_msg)
        return {"error": f"Worker error ({e.code}): {err_msg}"}
    except Exception as e:
        logger.error("Error communicating with worker: %s", e)
        return {"error": f"Communication error: {e}"}


def handle_client_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Routes action to worker API endpoints."""
    action = msg.get("action", "")

    if action == "ping":
        worker_info = ensure_worker_running()
        return {"status": "ok", "worker_pid": worker_info.get("pid"), "port": worker_info.get("port")}

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
        return send_worker_request("/jobs", method="GET")

    if action == "status":
        job_id = msg.get("job_id", "")
        return send_worker_request(f"/jobs/{job_id}", method="GET")

    if action == "cancel":
        job_id = msg.get("job_id", "")
        return send_worker_request("/cancel", method="POST", body={"job_id": job_id})

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
    try:
        while True:
            msg = read_message()
            if msg is None:
                logger.info("Native messaging client closed pipe. Exiting bridge.")
                break

            response = handle_client_message(msg)
            send_message(response)
    except Exception as e:
        logger.exception("Bridge encountered unexpected error: %s", e)
        try:
            send_message({"error": f"Bridge error: {e}"})
        except Exception:
            pass


if __name__ == "__main__":
    main()
