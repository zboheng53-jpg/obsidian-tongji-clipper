"""Data models and capture schema validation for Tongji Clipper."""

from __future__ import annotations

import dataclasses
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from tongji_clipper.config import assert_safe_id, sanitize_url


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_REFRESH = "needs_refresh"


def validate_signed_url(url: str, stream_id: str) -> None:
    """
    Strictly validates media stream URL format:
    - https scheme
    - domain look.tongji.edu.cn
    - path starts with /play/
    - no userinfo (@)
    - default/standard port only (no abnormal ports)
    NEVER output raw signature in exceptions!
    """
    if not url:
        raise ValueError(f"Stream '{stream_id}' signed_url cannot be empty")
    try:
        p = urlparse(url)
    except Exception:
        raise ValueError(f"Stream '{stream_id}' signed_url is malformed")

    if p.scheme != "https":
        raise ValueError(f"Stream '{stream_id}' signed_url must use https scheme")
    if "@" in p.netloc:
        raise ValueError(f"Stream '{stream_id}' signed_url contains forbidden userinfo")
    if p.hostname != "look.tongji.edu.cn":
        raise ValueError(f"Stream '{stream_id}' signed_url host must be look.tongji.edu.cn")
    if p.port is not None and p.port != 443:
        raise ValueError(f"Stream '{stream_id}' signed_url uses abnormal port: {p.port}")
    if not p.path.startswith("/play/"):
        raise ValueError(f"Stream '{stream_id}' signed_url path must start with /play/, got {p.path}")


@dataclasses.dataclass
class MediaStream:
    stream_id: str
    signed_url: str
    time_offset: float = 0.0

    def validate(self, is_local: bool = False) -> None:
        assert_safe_id(self.stream_id, "stream_id")
        if not math.isfinite(self.time_offset) or self.time_offset < 0:
            raise ValueError("time_offset must be a finite nonnegative number")
        if is_local and (not Path(self.signed_url).is_absolute() or not Path(self.signed_url).is_file()):
            raise ValueError("Local stream must be an existing absolute file path")
        if not is_local:
            validate_signed_url(self.signed_url, self.stream_id)

    def to_dict(self, include_signature: bool = True) -> Dict[str, Any]:
        data = {
            "stream_id": self.stream_id,
            "time_offset": self.time_offset,
        }
        if include_signature:
            data["signed_url"] = self.signed_url
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MediaStream:
        return cls(
            stream_id=str(data.get("stream_id", "default")),
            signed_url=str(data.get("signed_url", "")),
            time_offset=float(data.get("time_offset", 0.0)),
        )


@dataclasses.dataclass
class CapturePayload:
    schema_version: int
    tenant_code: str
    course_id: str
    sub_id: str
    source_url: str
    course_title: Optional[str] = None
    sub_title: Optional[str] = None
    lecturer_name: Optional[str] = None
    room_name: Optional[str] = None
    media_streams: List[MediaStream] = dataclasses.field(default_factory=list)
    local_file_path: Optional[str] = None

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError(f"Unsupported schema_version: {self.schema_version}, expected 1")

        assert_safe_id(self.tenant_code, "tenant_code")
        assert_safe_id(self.course_id, "course_id")
        assert_safe_id(self.sub_id, "sub_id")

        if not self.media_streams:
            raise ValueError("No media streams provided")

        is_local = self.tenant_code == "local"

        if is_local:
            if not self.local_file_path:
                # Use signed_url of first stream as local file path
                self.local_file_path = self.media_streams[0].signed_url
            p = Path(self.local_file_path).resolve()
            if not p.is_file():
                raise ValueError(f"Local media file does not exist: {p}")
            for s in self.media_streams:
                s.validate(is_local=True)
            return

        # Remote validation: strictly livingroom page
        parsed = urlparse(self.source_url)
        if parsed.scheme != "https" or parsed.netloc != "look.tongji.edu.cn":
            raise ValueError(
                f"source_url domain must be https://look.tongji.edu.cn, got {sanitize_url(self.source_url)}"
            )
        if parsed.path != "/livingroom":
            raise ValueError(
                f"source_url path must be /livingroom, got {parsed.path}"
            )

        query = parse_qs(parsed.query)
        q_tenant = query.get("tenant_code", [""])[0]
        q_course = query.get("course_id", [""])[0]
        q_sub = query.get("sub_id", [""])[0]

        if not q_tenant or not q_course or not q_sub:
            raise ValueError("source_url query parameters must contain tenant_code, course_id, and sub_id")

        if q_tenant != self.tenant_code or q_course != self.course_id or q_sub != self.sub_id:
            raise ValueError(
                f"source_url query parameters do not match payload IDs: "
                f"url({q_tenant}/{q_course}/{q_sub}) vs payload({self.tenant_code}/{self.course_id}/{self.sub_id})"
            )

        for s in self.media_streams:
            s.validate(is_local=False)

    def to_dict(self, include_signature: bool = True) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tenant_code": self.tenant_code,
            "course_id": self.course_id,
            "sub_id": self.sub_id,
            "source_url": self.source_url,
            "course_title": self.course_title,
            "sub_title": self.sub_title,
            "lecturer_name": self.lecturer_name,
            "room_name": self.room_name,
            "media_streams": [s.to_dict(include_signature=include_signature) for s in self.media_streams],
            "local_file_path": self.local_file_path,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CapturePayload:
        streams = [
            MediaStream.from_dict(item) for item in data.get("media_streams", [])
        ]
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            tenant_code=str(data.get("tenant_code", "")),
            course_id=str(data.get("course_id", "")),
            sub_id=str(data.get("sub_id", "")),
            source_url=str(data.get("source_url", "")),
            course_title=data.get("course_title"),
            sub_title=data.get("sub_title"),
            lecturer_name=data.get("lecturer_name"),
            room_name=data.get("room_name"),
            media_streams=streams,
            local_file_path=data.get("local_file_path"),
        )

    @classmethod
    def create_local(
        cls,
        file_path: str,
        title: Optional[str] = None,
        lecturer: Optional[str] = None,
        time_offset: float = 0.0,
    ) -> CapturePayload:
        p = Path(file_path).resolve()
        if not p.is_file():
            raise ValueError(f"Local file does not exist: {p}")

        stem = p.stem
        # Generate stable path hash to distinguish same filename in different directories
        path_hash = hashlib.sha256(str(p).encode("utf-8")).hexdigest()[:8]
        safe_stem = "".join(c if c.isascii() and (c.isalnum() or c in "_-.") else "_" for c in stem)[:40]
        course_id = "local"
        sub_id = f"{safe_stem}_{path_hash}"
        stream_id = f"stream_offset_{int(time_offset)}"

        return cls(
            schema_version=1,
            tenant_code="local",
            course_id=course_id,
            sub_id=sub_id,
            source_url=f"file://{p.as_posix()}",
            course_title=title or stem,
            sub_title=stem,
            lecturer_name=lecturer,
            room_name="Local Machine",
            media_streams=[
                MediaStream(
                    stream_id=stream_id,
                    signed_url=str(p),
                    time_offset=time_offset,
                )
            ],
            local_file_path=str(p),
        )


@dataclasses.dataclass
class JobConfig:
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    language: str = "zh"
    chunk_duration: float = 300.0
    chunk_overlap: float = 10.0
    keep_audio: bool = False
    offline: bool = False
    model_dir: Optional[str] = None
    output_dir: Optional[str] = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.chunk_duration) or self.chunk_duration <= 0:
            raise ValueError("chunk_duration must be positive and finite")
        if not math.isfinite(self.chunk_overlap) or not 0 <= self.chunk_overlap < self.chunk_duration:
            raise ValueError("chunk_overlap must be finite and less than chunk_duration")

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> JobConfig:
        return cls(
            model=str(data.get("model", "small")),
            device=str(data.get("device", "cpu")),
            compute_type=str(data.get("compute_type", "int8")),
            language=str(data.get("language", "zh")),
            chunk_duration=float(data.get("chunk_duration", 300.0)),
            chunk_overlap=float(data.get("chunk_overlap", 10.0)),
            keep_audio=bool(data.get("keep_audio", False)),
            offline=bool(data.get("offline", False)),
            model_dir=data.get("model_dir"),
            output_dir=data.get("output_dir"),
        )


def compute_idempotency_key(
    tenant_code: str,
    course_id: str,
    sub_id: str,
    stream_id: str,
    time_offset: float,
    config: JobConfig,
    local_file_path: Optional[str] = None,
) -> str:
    """
    Computes deterministic idempotency key.
    Includes content-affecting parameters (model, device, language, chunking, time_offset, local path identity, output_dir, keep_audio).
    IMPORTANT: Must NOT include signed_url query tokens/signatures!
    """
    local_part = ""
    if local_file_path:
        p = Path(local_file_path).resolve()
        stat_size = p.stat().st_size if p.is_file() else 0
        local_part = f":{p.as_posix()}:{stat_size}:{p.stat().st_mtime_ns if p.is_file() else 0}"

    seed = (
        f"{tenant_code}:{course_id}:{sub_id}:{stream_id}:{time_offset:.3f}:"
        f"{config.model}:{config.device}:{config.compute_type}:{config.language}:"
        f"{config.chunk_duration:.1f}:{config.chunk_overlap:.1f}:"
        f"{config.model_dir}:{config.offline}:{config.output_dir}:{config.keep_audio}{local_part}"
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def compute_job_id(idempotency_key: str) -> str:
    """Stable job id derived from idempotency key."""
    return f"job_{idempotency_key[:16]}"
