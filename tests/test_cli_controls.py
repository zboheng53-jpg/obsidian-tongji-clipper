"""CLI control requests report acceptance without claiming execution stopped."""

import contextlib
import io
import unittest
from unittest.mock import patch

from tongji_clipper import cli


class TestCLIControls(unittest.TestCase):
    def test_control_routes(self):
        for action in ("pause", "resume", "retry"):
            with self.subTest(action=action), patch("sys.argv", ["clipper", action, "job_demo"]), patch.object(cli, "send_worker_request", return_value={"status": "ok"}) as request, contextlib.redirect_stdout(io.StringIO()):
                cli.main()
                request.assert_called_once_with(f"/{action}", method="POST", body={"job_id": "job_demo"})

    def test_cancel_reports_request_not_completion(self):
        output = io.StringIO()
        with patch("sys.argv", ["clipper", "cancel", "job_demo"]), patch.object(cli, "send_worker_request", return_value={"status": "ok", "cancelled": True}), contextlib.redirect_stdout(output):
            cli.main()
        self.assertIn("取消请求", output.getvalue())
        self.assertNotIn("已取消", output.getvalue())

    def test_rejected_control_exits_nonzero(self):
        with patch("sys.argv", ["clipper", "pause", "job_demo"]), patch.object(cli, "send_worker_request", return_value={"status": "ok", "paused": False}), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            cli.main()
        self.assertEqual(error.exception.code, 1)
