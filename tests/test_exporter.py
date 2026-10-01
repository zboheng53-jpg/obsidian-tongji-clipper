"""Tests for Markdown frontmatter, wikilink, #clip-t= timestamp links, SRT format, atomic file write, and revision handling."""

import json
import tempfile
import unittest
from pathlib import Path

from tongji_clipper.exporter import (
    build_markdown,
    build_srt,
    export_job_results,
    format_seconds_to_hms,
    format_seconds_to_srt_time,
    verify_exported_artifacts,
)


class TestExporter(unittest.TestCase):

    def setUp(self):
        self.sample_job = {
            "course_title": '信号与系统 "带引号与换行\n测试"',
            "sub_title": "连续时间傅里叶变换",
            "course_id": "EE202",
            "sub_id": "lesson_04",
            "tenant_code": "tongji",
            "lecturer_name": "张教授",
            "room_name": "致远楼205",
            "source_url": "https://look.tongji.edu.cn/livingroom?course_id=EE202&sub_id=lesson_04&tenant_code=tongji",
            "config": {
                "model": "small",
                "device": "cpu",
                "compute_type": "int8",
                "offline": True,
            },
        }

        self.sample_segments = [
            {"start": 15.0, "end": 20.5, "text": "各位同学请看黑板。"},
            {"start": 83.2, "end": 90.0, "text": "这是傅里叶变换的积分定义公式。"},
        ]
        self.sample_timings = {
            "acquisition": 2.5,
            "asr": 35.0,
            "export": 0.5,
            "total": 38.0,
        }

    def test_time_formatting(self):
        self.assertEqual(format_seconds_to_hms(83.2), "00:01:23")
        self.assertEqual(format_seconds_to_hms(3665), "01:01:05")
        self.assertEqual(format_seconds_to_srt_time(83.2), "00:01:23,200")

    def test_markdown_frontmatter_and_links(self):
        md = build_markdown(self.sample_job, self.sample_segments, self.sample_timings)

        # Frontmatter safely formatted with JSON serialization
        self.assertTrue(md.startswith("---"))
        self.assertIn('course_id: "EE202"', md)
        self.assertIn('lecturer_name: "张教授"', md)
        self.assertIn('offline: true', md)
        self.assertIn("acquisition_seconds: 2.50", md)
        self.assertIn("asr_seconds: 35.00", md)

        # Wikilink
        self.assertIn("[[张教授]]", md)

        # Timestamp link with #clip-t=
        self.assertIn("[00:00:15](https://look.tongji.edu.cn/livingroom?course_id=EE202&sub_id=lesson_04&tenant_code=tongji#clip-t=15)", md)
        self.assertIn("[00:01:23](https://look.tongji.edu.cn/livingroom?course_id=EE202&sub_id=lesson_04&tenant_code=tongji#clip-t=83)", md)
        self.assertIn("这是傅里叶变换的积分定义公式。", md)

    def test_srt_format(self):
        srt = build_srt(self.sample_segments)
        self.assertIn("1\n00:00:15,000 --> 00:00:20,500\n各位同学请看黑板。", srt)
        self.assertIn("2\n00:01:23,200 --> 00:01:30,000\n这是傅里叶变换的积分定义公式。", srt)

    def test_atomic_export_and_revision_handling(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out_test"
            paths1 = export_job_results(
                job_info=self.sample_job,
                segments=self.sample_segments,
                output_dir=out_dir,
                timings=self.sample_timings,
            )

            self.assertTrue(Path(paths1["markdown"]).is_file())
            self.assertTrue(Path(paths1["srt"]).is_file())
            self.assertTrue(Path(paths1["segments"]).is_file())

            # Exporting again creates revision without overwriting existing file
            paths2 = export_job_results(
                job_info=self.sample_job,
                segments=self.sample_segments,
                output_dir=out_dir,
                timings=self.sample_timings,
            )
            self.assertIn("_rev1", paths2["markdown"])
            self.assertTrue(Path(paths2["markdown"]).is_file())

            # Verify no temporary files remain
            tmp_files = list(out_dir.glob("*.tmp"))
            self.assertEqual(len(tmp_files), 0)


if __name__ == "__main__":
    unittest.main()
