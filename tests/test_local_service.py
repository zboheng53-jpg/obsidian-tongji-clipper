import importlib.util
import sys
import tempfile
import threading
import time
import unittest
import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("local_service", ROOT / "local_service.py")
SERVICE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = SERVICE
SPEC.loader.exec_module(SERVICE)


def payload():
    return {
        "page_url": "https://look.tongji.edu.cn/livingroom?course_id=1&sub_id=2&tenant_code=222",
        "video_url": "https://look.tongji.edu.cn/play/course.mp4?t=signed",
        "title": "测试课程 2026-07-15第1-2节",
        "course_name": "测试课程",
        "session": "2026-07-15第1-2节",
        "author": "测试教师",
        "published": "2026-07-15",
        "room": "测试教室",
        "class_time": "08:00-09:35",
    }


class ServiceTests(unittest.TestCase):
    def test_range_download_retries_after_read_timeout(self):
        class FakeResponse:
            def __init__(self, *, headers=None, status=206, blocks=None):
                self.headers = headers or {}
                self.status = status
                self.blocks = iter(blocks or [])

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                block = next(self.blocks, b"")
                if isinstance(block, Exception):
                    raise block
                return block

        responses = [
            FakeResponse(headers={"Content-Range": "bytes 0-0/4"}),
            FakeResponse(blocks=[TimeoutError("The read operation timed out")]),
            FakeResponse(blocks=[b"data"]),
        ]

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "course.mp4"
            with patch.object(SERVICE.urllib.request, "urlopen", side_effect=responses), patch.object(
                SERVICE.time, "sleep"
            ):
                SERVICE.download_video(
                    "https://look.tongji.edu.cn/play/course.mp4?t=signed",
                    "https://look.tongji.edu.cn/livingroom?course_id=1",
                    destination,
                )

            self.assertEqual(b"data", destination.read_bytes())

    def test_placeholder_vault_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(json.dumps({"vault_dir": r"C:\Path\To\Your\ObsidianVault"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "尚未配置"):
                SERVICE.Settings.load(config)

    def test_unconfigured_service_can_expose_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "vault_dir": r"C:\Path\To\Your\ObsidianVault",
                        "notes_subdir": "课程笔记",
                        "model": "small",
                    }
                ),
                encoding="utf-8",
            )
            manager = SERVICE.ConfigManager(config)
            self.assertFalse(manager.is_configured())
            self.assertEqual("", manager.public_settings()["vault_dir"])
            with self.assertRaisesRegex(ValueError, "尚未配置"):
                SERVICE.JobManager(manager).submit(payload())

    def test_absolute_output_directory_overrides_vault_subdirectory(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output = base / "任意位置" / "课程归档"
            output.mkdir(parents=True)
            config = base / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "vault_dir": r"C:\Path\To\Your\ObsidianVault",
                        "output_dir": str(output),
                    }
                ),
                encoding="utf-8",
            )
            manager = SERVICE.ConfigManager(config)

            self.assertTrue(manager.is_configured())
            self.assertEqual(output.resolve(), manager.require_settings().output_dir)
            self.assertEqual(output.resolve(), Path(manager.public_settings()["output_dir"]).resolve())

            other_output = base / "另一个库" / "转写"
            other_output.mkdir(parents=True)
            updated = manager.update({"output_dir": str(other_output)})
            self.assertEqual(other_output.resolve(), Path(updated["output_dir"]).resolve())

    def test_select_vault_save_settings_and_test_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            vault = base / "我的 Vault"
            vault.mkdir()
            (vault / ".obsidian").mkdir()
            config = base / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "vault_dir": r"C:\Path\To\Your\ObsidianVault",
                        "notes_subdir": "课程笔记",
                        "model": "small",
                        "keep_temporary_media": False,
                    }
                ),
                encoding="utf-8",
            )
            manager = SERVICE.ConfigManager(config)
            with patch.object(SERVICE, "select_vault_directory", return_value=str(vault)):
                selected = manager.select_vault()
            self.assertFalse(selected["cancelled"])
            self.assertTrue(selected["configured"])
            self.assertTrue(selected["looks_like_vault"])

            updated = manager.update(
                {
                    "notes_subdir": "课程笔记/自动转写",
                    "model": "base",
                    "keep_temporary_media": True,
                }
            )
            self.assertEqual("课程笔记/自动转写", updated["notes_subdir"])
            self.assertEqual("base", updated["model"])
            self.assertTrue(updated["keep_temporary_media"])

            result = manager.test_write()
            output = Path(result["output"])
            self.assertTrue(output.is_file())
            self.assertTrue(output.resolve().is_relative_to(vault.resolve()))
            reloaded = SERVICE.ConfigManager(config).public_settings()
            self.assertEqual(vault.resolve(), Path(reloaded["vault_dir"]).resolve())
            self.assertEqual("课程笔记/自动转写", reloaded["notes_subdir"])

    def test_auto_selects_the_most_recent_obsidian_vault(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            other_vault = base / "其他库"
            active_vault = base / "当前库"
            for vault in (other_vault, active_vault):
                vault.mkdir()
                (vault / ".obsidian").mkdir()
            obsidian_config = base / "obsidian.json"
            obsidian_config.write_text(
                json.dumps(
                    {
                        "vaults": {
                            "other": {"path": str(other_vault), "ts": 100},
                            "active": {"path": str(active_vault), "ts": 200},
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = base / "config.json"
            config.write_text(
                json.dumps({"vault_dir": r"C:\Path\To\Your\ObsidianVault"}),
                encoding="utf-8",
            )

            manager = SERVICE.ConfigManager(config)
            selected = manager.auto_select_vault(obsidian_config)

            self.assertTrue(selected["auto_selected"])
            self.assertTrue(selected["configured"])
            self.assertEqual(active_vault.resolve(), Path(selected["vault_dir"]).resolve())
            self.assertEqual(
                active_vault.resolve(),
                Path(SERVICE.ConfigManager(config).public_settings()["vault_dir"]).resolve(),
            )

    def test_settings_reject_path_traversal_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            vault = base / "vault"
            vault.mkdir()
            config = base / "config.json"
            config.write_text(json.dumps({"vault_dir": str(vault)}), encoding="utf-8")
            manager = SERVICE.ConfigManager(config)
            with self.assertRaisesRegex(ValueError, "相对路径"):
                manager.update({"notes_subdir": "../outside"})
            with self.assertRaisesRegex(ValueError, "不允许修改"):
                manager.update({"host": "0.0.0.0"})

    def test_cancelled_vault_selection_preserves_existing_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            vault = base / "vault"
            vault.mkdir()
            config = base / "config.json"
            config.write_text(json.dumps({"vault_dir": str(vault)}), encoding="utf-8")
            manager = SERVICE.ConfigManager(config)
            with patch.object(SERVICE, "select_vault_directory", return_value=None):
                selected = manager.select_vault()
            self.assertTrue(selected["cancelled"])
            self.assertEqual(str(vault), selected["vault_dir"])

    def test_http_configuration_flow_and_origin_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            vault = base / "vault"
            vault.mkdir()
            (vault / ".obsidian").mkdir()
            config = base / "config.json"
            config.write_text(
                json.dumps({"vault_dir": r"C:\Path\To\Your\ObsidianVault"}),
                encoding="utf-8",
            )
            config_manager = SERVICE.ConfigManager(config)
            SERVICE.ApiHandler.config_manager = config_manager
            SERVICE.ApiHandler.manager = SERVICE.JobManager(config_manager)
            server = SERVICE.ThreadingHTTPServer(("127.0.0.1", 0), SERVICE.ApiHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            extension_headers = {
                "Origin": "chrome-extension://test-extension",
                "X-Tongji-Clipper": "1",
            }
            edge_extension_headers = {
                "Origin": "edge-extension://test-extension",
                "X-Tongji-Clipper": "1",
            }
            microsoft_edge_headers = {
                "Origin": "extension://test-extension",
                "X-Tongji-Clipper": "1",
            }
            opaque_edge_headers = {
                "Origin": "null",
                "X-Tongji-Clipper": "1",
            }

            def request(path, method="GET", body=None, headers=None):
                data = None if body is None else json.dumps(body).encode("utf-8")
                merged = dict(headers or {})
                if data is not None:
                    merged["Content-Type"] = "application/json"
                response = urllib.request.urlopen(
                    urllib.request.Request(
                        base_url + path,
                        data=data,
                        headers=merged,
                        method=method,
                    ),
                    timeout=5,
                )
                return response.status, json.loads(response.read().decode("utf-8"))

            try:
                status, health = request("/health")
                self.assertEqual(200, status)
                self.assertFalse(health["configured"])

                status, _settings = request("/settings", headers=edge_extension_headers)
                self.assertEqual(200, status)

                status, _settings = request("/settings", headers=microsoft_edge_headers)
                self.assertEqual(200, status)

                status, jobs = request("/jobs", headers=extension_headers)
                self.assertEqual(200, status)
                self.assertEqual([], jobs)

                status, _settings = request("/settings", headers=opaque_edge_headers)
                self.assertEqual(200, status)

                with self.assertRaises(urllib.error.HTTPError) as forbidden:
                    request(
                        "/settings",
                        headers={
                            "Origin": "https://example.com",
                            "X-Tongji-Clipper": "1",
                        },
                    )
                self.assertEqual(403, forbidden.exception.code)

                with patch.object(
                    SERVICE, "find_active_obsidian_vault", return_value=vault
                ):
                    status, auto_selected = request(
                        "/settings/auto-select-vault",
                        method="POST",
                        headers=extension_headers,
                    )
                self.assertEqual(200, status)
                self.assertTrue(auto_selected["auto_selected"])
                self.assertTrue(auto_selected["configured"])

                with patch.object(SERVICE, "select_vault_directory", return_value=str(vault)):
                    status, selected = request(
                        "/settings/select-vault",
                        method="POST",
                        headers=extension_headers,
                    )
                self.assertEqual(200, status)
                self.assertTrue(selected["configured"])

                status, updated = request(
                    "/settings",
                    method="PUT",
                    body={
                        "notes_subdir": "课程笔记/自动转写",
                        "model": "small",
                        "keep_temporary_media": False,
                    },
                    headers=extension_headers,
                )
                self.assertEqual(200, status)
                self.assertEqual("课程笔记/自动转写", updated["notes_subdir"])

                status, result = request(
                    "/settings/test-write",
                    method="POST",
                    headers=extension_headers,
                )
                self.assertEqual(200, status)
                self.assertTrue(Path(result["output"]).is_file())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_text_cleanup_removes_replacement_character(self):
        self.assertEqual("测试 文本", SERVICE.compact_whitespace("测试\ufffd 文本"))

    def test_text_cleanup_normalizes_simplified_chinese_and_punctuation(self):
        self.assertEqual("上课，请坐！", SERVICE.compact_whitespace("上課,請坐!"))

    def test_rejects_non_tongji_urls(self):
        bad = payload()
        bad["video_url"] = "https://example.com/video.mp4"
        with self.assertRaises(ValueError):
            SERVICE.validate_payload(bad)

    def test_markdown_is_deterministic_and_timestamped(self):
        note = SERVICE.render_markdown(
            SERVICE.validate_payload(payload()),
            [
                {"start": 0.0, "end": 5.0, "text": "第一段"},
                {"start": 40.0, "end": 45.0, "text": "第二段"},
            ],
        )
        self.assertIn("source: \"https://look.tongji.edu.cn/livingroom", note)
        self.assertIn("**0:00** · 第一段。", note)
        self.assertIn("**0:40** · 第二段。", note)
        self.assertIn("未调用 Agent、云端 LLM", note)

    def test_job_runs_without_llm_and_writes_note(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            settings = SERVICE.Settings(
                vault_dir=base / "vault",
                notes_subdir="课程笔记",
                model="small",
                language="zh",
                host="127.0.0.1",
                port=8765,
                keep_temporary_media=False,
                model_cache_dir=base / "models",
            )
            manager = SERVICE.JobManager(settings)

            def fake_download(_video_url, _page_url, destination):
                destination.write_bytes(b"fake media")

            fake_segments = [{"start": 0.0, "end": 2.0, "text": "自动化测试"}]
            with patch.object(SERVICE, "download_video", side_effect=fake_download), patch.object(
                SERVICE, "transcribe_video", return_value=fake_segments
            ):
                job = manager.submit(payload())
                deadline = time.time() + 5
                while time.time() < deadline:
                    current = manager.get(job["id"])
                    if current["state"] in {"completed", "failed"}:
                        break
                    time.sleep(0.02)

            self.assertEqual("completed", current["state"], current)
            output = Path(current["output"])
            self.assertTrue(output.is_file())
            self.assertIn("自动化测试", output.read_text(encoding="utf-8"))

    def test_interrupted_job_is_persisted_as_failed_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state_path = base / "jobs.json"
            state_path.write_text(
                json.dumps(
                    {
                        "lost-job": {
                            "id": "lost-job",
                            "state": "transcribing",
                            "page_url": "https://look.tongji.edu.cn/livingroom?course_id=1",
                            "title": "中断的课程",
                            "created_at": 100,
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings = SERVICE.Settings(
                vault_dir=base,
                notes_subdir="课程笔记",
                model="small",
                language="zh",
                host="127.0.0.1",
                port=8765,
                keep_temporary_media=False,
                model_cache_dir=base / "models",
            )

            manager = SERVICE.JobManager(settings, state_path=state_path)
            restored = manager.get("lost-job")

            self.assertEqual("failed", restored["state"])
            self.assertIn("服务重启", restored["error"])
            self.assertEqual("lost-job", manager.list()[0]["id"])
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual("failed", persisted["lost-job"]["state"])

    def test_source_has_no_cloud_llm_client(self):
        source = (ROOT / "local_service.py").read_text(encoding="utf-8").lower()
        forbidden = ["openai", "anthropic", "gemini", "chatcompletion", "responses.create"]
        self.assertFalse([term for term in forbidden if term in source])


if __name__ == "__main__":
    unittest.main()
