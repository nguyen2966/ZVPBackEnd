"""Read uploaded video properties and validate them against app configuration."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .upload_config import UploadConfiguration


@dataclass(frozen=True)
class VideoMetadata:
    duration_ms: int
    video_bit_rate: int
    frames_per_second: float
    width: int
    height: int


def probe_video_metadata(source: Path) -> VideoMetadata | None:
    process = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type,width,height,avg_frame_rate,bit_rate",
            "-show_entries", "format=duration,bit_rate",
            "-of", "json",
            str(source),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if process.returncode != 0:
        return None

    try:
        data = json.loads(process.stdout)
        stream = next(
            item
            for item in data.get("streams", [])
            if item.get("codec_type") == "video"
        )
        format_data = data["format"]
        video_bit_rate = stream.get("bit_rate") or format_data.get("bit_rate")
        frames_per_second = float(Fraction(stream["avg_frame_rate"]))
        metadata = VideoMetadata(
            duration_ms=int(float(format_data["duration"]) * 1000),
            video_bit_rate=int(video_bit_rate),
            frames_per_second=frames_per_second,
            width=int(stream["width"]),
            height=int(stream["height"]),
        )
    except (KeyError, StopIteration, TypeError, ValueError, ZeroDivisionError):
        return None

    if any(
        value <= 0
        for value in (
            metadata.duration_ms,
            metadata.video_bit_rate,
            metadata.frames_per_second,
            metadata.width,
            metadata.height,
        )
    ):
        return None
    return metadata


def validation_errors(
    metadata: VideoMetadata,
    configuration: UploadConfiguration,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if metadata.duration_ms > configuration.max_duration_seconds * 1000:
        errors.append({
            "field": "file",
            "rule": "max_duration",
            "message": (
                f"Video phải dài tối đa {configuration.max_duration_seconds} giây"
            ),
        })
    if metadata.video_bit_rate > configuration.max_video_bit_rate:
        errors.append({
            "field": "file",
            "rule": "max_video_bit_rate",
            "message": (
                "Video vượt quá bitrate tối đa "
                f"{configuration.max_video_bit_rate} bit/giây"
            ),
        })
    if metadata.frames_per_second > configuration.max_frames_per_second + 0.01:
        errors.append({
            "field": "file",
            "rule": "max_frames_per_second",
            "message": (
                "Video vượt quá tốc độ khung hình tối đa "
                f"{configuration.max_frames_per_second} fps"
            ),
        })

    video_edges = sorted((metadata.width, metadata.height))
    configured_edges = sorted((
        configuration.max_resolution_width,
        configuration.max_resolution_height,
    ))
    if any(actual > maximum for actual, maximum in zip(video_edges, configured_edges)):
        errors.append({
            "field": "file",
            "rule": "max_resolution",
            "message": (
                "Video vượt quá độ phân giải tối đa "
                f"{configuration.max_resolution_width}x"
                f"{configuration.max_resolution_height}"
            ),
        })
    return errors
