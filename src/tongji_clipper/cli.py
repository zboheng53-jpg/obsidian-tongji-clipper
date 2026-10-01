"""Command-line interface for Tongji Clipper."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, Optional

from tongji_clipper.config import get_default_output_dir, load_worker_info
from tongji_clipper.models import CapturePayload, JobConfig, JobStatus
from tongji_clipper.native_host.bridge import ensure_worker_running, send_worker_request


def _format_job_row(job: Dict[str, Any]) -> str:
    jid = job.get("job_id", "")
    status = job.get("status", "")
    title = job.get("course_title") or job.get("sub_title") or "-"
    teacher = job.get("lecturer_name") or "-"
    pct = ""
    prog = job.get("progress_info")
    if isinstance(prog, dict):
        value = prog.get("chunk_percent", prog.get("percent"))
        if isinstance(value, (int, float)) and math.isfinite(value):
            pct = f" ({value}%)"
    err = f" [ERR: {job['error_message']}]" if job.get("error_message") else ""
    out = f" -> {job['output_dir']}" if job.get("output_dir") else ""
    return f"[{jid}] {status.upper()}{pct} | 课程: {title} | 教师: {teacher}{out}{err}"


def cmd_submit(args: argparse.Namespace) -> None:
    input_path = Path(args.input).resolve()
    if not input_path.exists():
        print(f"错误: 找不到输入文件 '{args.input}'", file=sys.stderr)
        sys.exit(1)

    # Detect if input is JSON capture or media file
    if input_path.suffix.lower() == ".json":
        with open(input_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        capture = CapturePayload.from_dict(data)
    else:
        capture = CapturePayload.create_local(
            file_path=str(input_path),
            title=args.title,
            lecturer=args.lecturer,
            time_offset=args.time_offset,
        )

    config = JobConfig(
        model=args.model,
        device=args.device,
        compute_type=args.compute_type,
        output_dir=str(Path(args.output_dir).resolve()) if args.output_dir else str(get_default_output_dir()),
        keep_audio=args.keep_audio,
        offline=args.offline,
        model_dir=args.model_dir,
        resource_profile=args.resource_profile,
    )

    print(f"[CLI] 正在提交任务 (模型: {config.model}, 离线模式: {config.offline})...")
    stream_id = args.stream_id or (capture.media_streams[0].stream_id if capture.media_streams else "")

    res = send_worker_request(
        "/submit",
        method="POST",
        body={
            "capture": capture.to_dict(include_signature=True),
            "stream_id": stream_id,
            "config": config.to_dict(),
        },
    )

    if "error" in res:
        print(f"提交失败: {res['error']}", file=sys.stderr)
        sys.exit(1)

    job = res.get("job", {})
    print(f"任务提交成功！\n{_format_job_row(job)}")

    if args.watch:
        job_id = job.get("job_id")
        print(f"正在实时跟踪任务状态 [{job_id}] (Ctrl+C 退出)...")
        while True:
            time.sleep(2.0)
            status_res = send_worker_request(f"/jobs/{job_id}", method="GET")
            if status_res.get("error"):
                print(f"\n状态暂不可用: {status_res['error']}", file=sys.stderr)
                continue
            j = status_res.get("job", {})
            st = j.get("status")
            print(f"\r{_format_job_row(j)}", end="", flush=True)
            if st in (JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value, JobStatus.NEEDS_REFRESH.value, JobStatus.PAUSED.value):
                print()
                if st != JobStatus.COMPLETED.value:
                    print(f"任务结束但未完成: 状态={st}, 错误={j.get('error_message')}", file=sys.stderr)
                    sys.exit(1)
                break


def cmd_status(args: argparse.Namespace) -> None:
    if args.job_id:
        res = send_worker_request(f"/jobs/{args.job_id}", method="GET")
        if "error" in res:
            print(f"获取状态失败: {res['error']}", file=sys.stderr)
            sys.exit(1)
        job = res.get("job")
        if not job:
            print(f"未找到任务 {args.job_id}")
            return
        print(_format_job_row(job))
        prog = job.get("progress_info")
        if prog:
            print(f"进度详情: {prog}")
        if job.get("output_dir"):
            print(f"产物目录: {job['output_dir']}")
    else:
        res = send_worker_request("/jobs", method="GET")
        if "error" in res:
            print(f"获取任务列表失败: {res['error']}", file=sys.stderr)
            sys.exit(1)
        jobs = res.get("jobs", [])
        if not jobs:
            print("当前暂无任务记录")
            return
        print(f"共找到 {len(jobs)} 个任务：")
        for j in jobs:
            print(_format_job_row(j))


def cmd_cancel(args: argparse.Namespace) -> None:
    res = send_worker_request("/cancel", method="POST", body={"job_id": args.job_id})
    if "error" in res:
        print(f"取消失败: {res['error']}", file=sys.stderr)
        sys.exit(1)
    if res.get("cancelled") is False:
        print("任务当前无法取消，请查询最新状态", file=sys.stderr)
        sys.exit(1)
    print(f"已发送任务 {args.job_id} 的取消请求；以任务状态确认为准")


def cmd_control(args: argparse.Namespace) -> None:
    res = send_worker_request(f"/{args.command}", method="POST", body={"job_id": args.job_id})
    if res.get("error") or res.get("status") != "ok" or res.get("paused") is False:
        print(f"操作失败: {res.get('error') or '任务当前不允许此操作'}", file=sys.stderr)
        sys.exit(1)
    if res.get("job"):
        print(_format_job_row(res["job"]))
    else:
        print(f"操作请求已发送，请通过 status {args.job_id} 查看确认状态")


def cmd_refresh(args: argparse.Namespace) -> None:
    res = send_worker_request(
        "/refresh_signature",
        method="POST",
        body={"job_id": args.job_id, "signed_url": args.signed_url},
    )
    if "error" in res:
        print(f"刷新签名失败: {res['error']}", file=sys.stderr)
        sys.exit(1)
    job = res.get("job", {})
    print(f"签名刷新成功，任务已重新进入队列！\n{_format_job_row(job)}")


def cmd_worker(args: argparse.Namespace) -> None:
    action = args.worker_action
    if action == "start":
        if args.daemon:
            info = ensure_worker_running()
            print(f"后台 Worker 已启动 (PID: {info.get('pid')}, Port: {info.get('port')})")
        else:
            from tongji_clipper.worker.server import run_server
            run_server(port=args.port)
    elif action == "stop":
        res = send_worker_request("/shutdown", method="POST")
        if res.get("error"):
            print(f"停止失败: {res['error']}", file=sys.stderr)
            sys.exit(1)
        print("已向 Worker 发送关闭信号")
    elif action == "status":
        info = load_worker_info()
        if not info:
            print("Worker 未运行 (未找到 worker.json)")
            return
        from tongji_clipper.native_host.bridge import ping_worker
        alive = ping_worker(int(info["port"]))
        if alive:
            print(f"Worker 运行中 (PID: {info.get('pid')}, Port: {info.get('port')})")
        else:
            print(f"Worker 状态异常或已退出 (记录 PID: {info.get('pid')})")


def cmd_verify_offline(args: argparse.Namespace) -> None:
    model_name = args.model
    print(f"正在验证模型 '{model_name}' 的离线可用性...")
    from tongji_clipper.asr import ASREngine
    engine = ASREngine(model_name=model_name, offline=True)
    try:
        engine.load_model()
        print(f"✅ 验证通过：模型 '{model_name}' 已就绪，可以在无网络环境下离线运行。")
    except Exception as e:
        print(f"❌ 验证未通过：离线加载失败 - {e}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="tongji-clipper", description="同济课程逐字稿本地工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # submit
    p_submit = subparsers.add_parser("submit", help="提交转写任务（支持本地音视频或Capture JSON）")
    p_submit.add_argument("input", help="本地音视频文件路径 (.mp4/.wav/.mp3 等) 或 capture.json 文件")
    p_submit.add_argument("--model", default="small", help="Faster-Whisper 模型名称或本地路径 (默认: small)")
    p_submit.add_argument("--model-dir", default=None, help="自定义模型本地缓存目录")
    p_submit.add_argument("--device", default="cpu", help="计算设备 (默认: cpu)")
    p_submit.add_argument("--compute-type", default="int8", help="计算精度 (默认: int8)")
    p_submit.add_argument("--resource-profile", choices=["low", "balanced", "fast"], default="balanced", help="资源档位：低占用、均衡、快速")
    p_submit.add_argument("--output-dir", default=None, help="导出目录 (默认: ./output)")
    p_submit.add_argument("--time-offset", type=float, default=0.0, help="起始时间偏移量 (秒)")
    p_submit.add_argument("--stream-id", default=None, help="指定媒体流 ID")
    p_submit.add_argument("--title", default=None, help="课程标题（本地文件时可选）")
    p_submit.add_argument("--lecturer", default=None, help="主讲教师（本地文件时可选）")
    p_submit.add_argument("--keep-audio", action="store_true", help="保留提取的临时 16kHz WAV 音频文件")
    p_submit.add_argument("--offline", action="store_true", help="强制离线模式（仅加载本地模型）")
    p_submit.add_argument("--watch", action="store_true", help="实时跟踪转写进度")
    p_submit.set_defaults(func=cmd_submit)

    # status
    p_status = subparsers.add_parser("status", help="查看任务状态")
    p_status.add_argument("job_id", nargs="?", default=None, help="任务 ID (留空显示所有任务)")
    p_status.set_defaults(func=cmd_status)

    # cancel
    p_cancel = subparsers.add_parser("cancel", help="取消任务")
    p_cancel.add_argument("job_id", help="任务 ID")
    p_cancel.set_defaults(func=cmd_cancel)

    for action, help_text in (("pause", "暂停任务并保留断点"), ("resume", "继续已暂停任务"), ("retry", "重试失败或取消的任务")):
        control_parser = subparsers.add_parser(action, help=help_text)
        control_parser.add_argument("job_id", help="任务 ID")
        control_parser.set_defaults(func=cmd_control)

    # refresh
    p_refresh = subparsers.add_parser("refresh", help="更新已失效的媒体签名 URL 并重新入队")
    p_refresh.add_argument("--job-id", required=True, help="任务 ID")
    p_refresh.add_argument("--signed-url", required=True, help="新获取的带签名 MP4 播放地址")
    p_refresh.set_defaults(func=cmd_refresh)

    # worker
    p_worker = subparsers.add_parser("worker", help="管理后台持久 Worker")
    p_worker.add_argument("worker_action", choices=["start", "stop", "status"], help="动作")
    p_worker.add_argument("--port", type=int, default=48721, help="绑定端口")
    p_worker.add_argument("--daemon", action="store_true", help="以独立后台进程启动")
    p_worker.set_defaults(func=cmd_worker)

    # verify-offline
    p_verify = subparsers.add_parser("verify-offline", help="验证指定模型离线可用性")
    p_verify.add_argument("--model", default="small", help="模型名称或本地路径")
    p_verify.set_defaults(func=cmd_verify_offline)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
