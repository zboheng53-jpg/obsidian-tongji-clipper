"""Tests for audio acquisition parameters, security boundaries, and credential scanning."""

import inspect
import re
import tempfile
import unittest
from pathlib import Path

from tongji_clipper.acquisition import extract_pcm_wav
from tongji_clipper.config import assert_safe_id, sanitize_url
from tongji_clipper.models import CapturePayload, MediaStream, validate_signed_url


class TestAcquisitionAndSecurity(unittest.TestCase):

    def test_ffmpeg_never_invoked_with_shell_true(self):
        """Inspects extract_pcm_wav source code to ensure shell=True is never used."""
        source = inspect.getsource(extract_pcm_wav)
        self.assertNotIn("shell=True", source, "FFmpeg must NEVER be invoked with shell=True")
        self.assertIn("subprocess.Popen", source)
        self.assertIn("-nostdin", source)
        self.assertIn("-map", source)
        self.assertIn("0:a:0", source)
        self.assertIn("-rw_timeout", source)
        # Verify -ss is NOT present in extract_pcm_wav (time_offset is not applied via -ss)
        self.assertNotIn('"-ss"', source)

    def test_capture_payload_livingroom_url_structure(self):
        """Ensures that only https://look.tongji.edu.cn/livingroom?course_id=... URLs are allowed."""
        # Non-livingroom path
        with self.assertRaises(ValueError):
            p = CapturePayload(
                schema_version=1,
                tenant_code="tongji",
                course_id="c1",
                sub_id="s1",
                source_url="https://look.tongji.edu.cn/play/c1/s1",
                media_streams=[MediaStream(stream_id="1", signed_url="https://look.tongji.edu.cn/play/video.mp4")],
            )
            p.validate()

        # Missing query parameters
        with self.assertRaises(ValueError):
            p = CapturePayload(
                schema_version=1,
                tenant_code="tongji",
                course_id="c1",
                sub_id="s1",
                source_url="https://look.tongji.edu.cn/livingroom",
                media_streams=[MediaStream(stream_id="1", signed_url="https://look.tongji.edu.cn/play/video.mp4")],
            )
            p.validate()

        # Mismatched query parameters vs payload IDs
        with self.assertRaises(ValueError):
            p = CapturePayload(
                schema_version=1,
                tenant_code="tongji",
                course_id="c1",
                sub_id="s1",
                source_url="https://look.tongji.edu.cn/livingroom?course_id=WRONG&sub_id=s1&tenant_code=tongji",
                media_streams=[MediaStream(stream_id="1", signed_url="https://look.tongji.edu.cn/play/video.mp4")],
            )
            p.validate()

        # Valid livingroom URL matching payload IDs
        valid = CapturePayload(
            schema_version=1,
            tenant_code="tongji",
            course_id="c1",
            sub_id="s1",
            source_url="https://look.tongji.edu.cn/livingroom?course_id=c1&sub_id=s1&tenant_code=tongji",
            media_streams=[MediaStream(stream_id="1", signed_url="https://look.tongji.edu.cn/play/video.mp4?token=sig123")],
        )
        valid.validate()

    def test_signed_url_strict_validation(self):
        """Ensures signed_url must be https, look.tongji.edu.cn, start with /play/, and reject userinfo or weird ports."""
        # Valid
        validate_signed_url("https://look.tongji.edu.cn/play/stream.mp4?sig=secret", "stream1")

        # Wrong domain
        with self.assertRaises(ValueError):
            validate_signed_url("https://evil.com/play/stream.mp4?sig=secret", "stream1")

        # Wrong path
        with self.assertRaises(ValueError):
            validate_signed_url("https://look.tongji.edu.cn/api/secret.mp4", "stream1")

        # Userinfo
        with self.assertRaises(ValueError):
            validate_signed_url("https://user:pass@look.tongji.edu.cn/play/stream.mp4", "stream1")

        # Abnormal port
        with self.assertRaises(ValueError):
            validate_signed_url("https://look.tongji.edu.cn:8443/play/stream.mp4", "stream1")

    def test_safe_id_validation(self):
        """Ensures traversal and special characters are rejected in IDs."""
        self.assertEqual(assert_safe_id("course_101.v2", "test"), "course_101.v2")
        with self.assertRaises(ValueError):
            assert_safe_id("../traversal", "test")
        with self.assertRaises(ValueError):
            assert_safe_id("course/sub", "test")
        with self.assertRaises(ValueError):
            assert_safe_id("course:101", "test")

    def test_sanitize_url(self):
        """Verifies query parameters and tokens are stripped."""
        raw = "https://look.tongji.edu.cn/play/video.mp4?token=SECRET_123&expire=999"
        sanitized = sanitize_url(raw)
        self.assertEqual(sanitized, "https://look.tongji.edu.cn/play/video.mp4")
        self.assertNotIn("SECRET_123", sanitized)

    def test_no_hardcoded_credentials_in_source_code(self):
        """Scans all python and javascript files for hardcoded tokens, passwords or secrets."""
        project_root = Path(__file__).resolve().parent.parent
        src_files = list((project_root / "src").rglob("*.py")) + list((project_root / "extension").rglob("*.js"))

        forbidden_patterns = [
            re.compile(r'(?i)(password|passwd|secret_key|api_key)\s*=\s*["\'][a-zA-Z0-9_\-]{8,}["\']'),
            re.compile(r'(?i)bearer\s+[a-zA-Z0-9_\-\.]{20,}'),
            re.compile(r'(?i)sessionid\s*='),
        ]

        for file_path in src_files:
            text = file_path.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                matches = pattern.findall(text)
                self.assertEqual(
                    len(matches),
                    0,
                    f"Possible hardcoded credential found in {file_path.name}: {matches}",
                )


if __name__ == "__main__":
    unittest.main()
