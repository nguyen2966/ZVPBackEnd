"""Upload và kiểm tra asset HLS V2 trên VNDATA S3-compatible storage."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
HLS_DIR = BASE_DIR / "public_v2" / "hls"
THUMB_DIR = BASE_DIR / "public_v2" / "thumbnails"

# Prefix trên S3 cho thumbnail. PHẢI là "thumbnails/" vì bucket policy chỉ mở
# public s3:GetObject cho hls/* và thumbnails/*; upload vào "thumbs/" sẽ trả 403.
THUMB_PREFIX = "thumbnails"

HLS_CONTENT_TYPE = "application/vnd.apple.mpegurl"
VIDEO_CONTENT_TYPE = "video/mp4"
IMAGE_CONTENT_TYPE = "image/jpeg"
PLAYLIST_CACHE = "public, max-age=300"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Thiếu biến môi trường {name} trong .env")
    return value


@dataclass(frozen=True)
class S3Settings:
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str
    public_base_url: str

    @classmethod
    def from_env(cls) -> "S3Settings":
        load_dotenv(BASE_DIR / ".env")
        endpoint = required_env("VNDATA_S3_ENDPOINT").rstrip("/")
        bucket = required_env("VNDATA_S3_BUCKET")
        public_base = os.environ.get("VNDATA_PUBLIC_BASE_URL", "").strip().rstrip("/")
        if not public_base:
            public_base = f"{endpoint}/{quote(bucket, safe='')}"
        return cls(
            endpoint=endpoint,
            bucket=bucket,
            access_key=required_env("VNDATA_S3_ACCESS_KEY"),
            secret_key=required_env("VNDATA_S3_SECRET_KEY"),
            region=os.environ.get("VNDATA_S3_REGION", "us-east-1").strip() or "us-east-1",
            public_base_url=public_base,
        )


def create_client(settings: S3Settings):
    """VNDATA documentation uses path-style URLs: endpoint/bucket/object."""
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint,
        aws_access_key_id=settings.access_key,
        aws_secret_access_key=settings.secret_key,
        region_name=settings.region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def public_url(settings: S3Settings, key: str) -> str:
    encoded_key = "/".join(quote(part, safe="") for part in key.split("/"))
    return f"{settings.public_base_url}/{encoded_key}"


def build_asset_urls(settings: S3Settings, video_id: str) -> dict[str, str]:
    return {
        "hls_url": public_url(settings, f"hls/{video_id}/master.m3u8"),
        "thumbnail_url": public_url(settings, f"{THUMB_PREFIX}/{video_id}.jpg"),
    }


def upload_thumbnail(video_id: str, content: bytes) -> str:
    """Upload JPEG do client cung cấp vào final thumbnail key của video."""
    settings = S3Settings.from_env()
    key = f"{THUMB_PREFIX}/{video_id}.jpg"
    create_client(settings).put_object(
        Bucket=settings.bucket,
        Key=key,
        Body=content,
        ContentType=IMAGE_CONTENT_TYPE,
        CacheControl=IMMUTABLE_CACHE,
    )
    return public_url(settings, key)


def upload_spec(path: Path, key: str, content_type: str, cache_control: str) -> tuple[Path, str, dict[str, str]]:
    return path, key, {"ContentType": content_type, "CacheControl": cache_control}


def hls_upload_plan(video_id: str) -> list[tuple[Path, str, dict[str, str]]]:
    """Các HLS object, với master playlist luôn nằm cuối."""
    video_dir = HLS_DIR / video_id
    master = video_dir / "master.m3u8"
    if not master.exists():
        raise FileNotFoundError(f"Thiếu {master}; chạy convert_v2.py trước")

    plan: list[tuple[Path, str, dict[str, str]]] = []
    for segment in sorted(video_dir.glob("*/*.mp4dv")):
        rel = segment.relative_to(video_dir).as_posix()
        plan.append(upload_spec(segment, f"hls/{video_id}/{rel}", VIDEO_CONTENT_TYPE, IMMUTABLE_CACHE))
    for playlist in sorted(video_dir.glob("*/index.m3u8")):
        rel = playlist.relative_to(video_dir).as_posix()
        plan.append(upload_spec(playlist, f"hls/{video_id}/{rel}", HLS_CONTENT_TYPE, PLAYLIST_CACHE))
    plan.append(upload_spec(master, f"hls/{video_id}/master.m3u8", HLS_CONTENT_TYPE, PLAYLIST_CACHE))
    return plan


def video_upload_plan(video_id: str) -> list[tuple[Path, str, dict[str, str]]]:
    """HLS cùng thumbnail local cho pipeline/legacy upload."""
    thumb = THUMB_DIR / f"{video_id}.jpg"
    if not thumb.exists():
        raise FileNotFoundError(f"Thiếu {thumb}; chạy convert_v2.py trước")

    hls_plan = hls_upload_plan(video_id)
    thumbnail = upload_spec(
        thumb,
        f"{THUMB_PREFIX}/{video_id}.jpg",
        IMAGE_CONTENT_TYPE,
        IMMUTABLE_CACHE,
    )
    return [*hls_plan[:-1], thumbnail, hls_plan[-1]]


def object_matches(client: Any, bucket: str, key: str, path: Path, extra: dict[str, str]) -> bool:
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status in {404, 403}:
            return False
        raise
    return (
        head.get("ContentLength") == path.stat().st_size
        and head.get("ContentType") == extra["ContentType"]
        and head.get("CacheControl") == extra["CacheControl"]
    )


def _upload_plan(
    video_id: str,
    plan: list[tuple[Path, str, dict[str, str]]],
    *,
    force: bool,
) -> dict[str, str]:
    settings = S3Settings.from_env()
    client = create_client(settings)
    uploaded = 0
    skipped = 0
    for path, key, extra in plan:
        if not force and object_matches(client, settings.bucket, key, path, extra):
            skipped += 1
            continue
        client.upload_file(str(path), settings.bucket, key, ExtraArgs=extra)
        uploaded += 1
    print(f"{video_id}: uploaded={uploaded}, skipped={skipped}")
    return build_asset_urls(settings, video_id)


def upload_video_assets(video_id: str, *, force: bool = False) -> dict[str, str]:
    return _upload_plan(video_id, video_upload_plan(video_id), force=force)


def upload_hls_assets(video_id: str, *, force: bool = False) -> dict[str, str]:
    """Upload HLS nhưng giữ nguyên thumbnail client đã upload lúc initialize."""
    return _upload_plan(video_id, hls_upload_plan(video_id), force=force)


def check_connection() -> None:
    settings = S3Settings.from_env()
    client = create_client(settings)
    client.head_bucket(Bucket=settings.bucket)
    print(f"Kết nối thành công: bucket={settings.bucket}, endpoint={settings.endpoint}")


def verify_video(video_id: str) -> None:
    """Xác minh object metadata và Range GET có trả 206/Content-Range."""
    settings = S3Settings.from_env()
    client = create_client(settings)
    master_key = f"hls/{video_id}/master.m3u8"
    master = client.head_object(Bucket=settings.bucket, Key=master_key)
    if master.get("ContentType") != HLS_CONTENT_TYPE:
        raise RuntimeError(f"Content-Type master không đúng: {master.get('ContentType')}")

    thumbnail_key = f"thumbnails/{video_id}.jpg"
    thumbnail = client.head_object(Bucket=settings.bucket, Key=thumbnail_key)
    if thumbnail.get("ContentType") != IMAGE_CONTENT_TYPE or int(thumbnail.get("ContentLength", 0)) <= 0:
        raise RuntimeError(
            f"Thumbnail không hợp lệ: Content-Type={thumbnail.get('ContentType')}, "
            f"Content-Length={thumbnail.get('ContentLength')}"
        )

    segment_keys = [key for _, key, _ in hls_upload_plan(video_id) if key.endswith(".mp4dv")]
    if not segment_keys:
        raise RuntimeError("Không tìm thấy rendition .mp4dv để kiểm tra")
    response = client.get_object(Bucket=settings.bucket, Key=segment_keys[0], Range="bytes=0-99")
    try:
        response["Body"].read()
    finally:
        response["Body"].close()
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if status != 206 or not response.get("ContentRange"):
        raise RuntimeError(f"Range GET không đạt: status={status}, ContentRange={response.get('ContentRange')}")
    print(
        f"Verify OK: master và thumbnail đúng metadata, "
        f"Range GET={status}, {response['ContentRange']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="VNDATA S3 uploader cho pipeline HLS V2")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Kiểm tra credentials và quyền truy cập bucket")
    upload = sub.add_parser("upload", help="Upload asset của một video")
    upload.add_argument("video_id")
    upload.add_argument("--force", action="store_true")
    verify = sub.add_parser("verify", help="Kiểm tra metadata và byte-range của video đã upload")
    verify.add_argument("video_id")
    args = parser.parse_args()

    if args.command == "check":
        check_connection()
    elif args.command == "upload":
        urls = upload_video_assets(args.video_id, force=args.force)
        print(f"HLS: {urls['hls_url']}")
        print(f"Thumbnail: {urls['thumbnail_url']}")
    elif args.command == "verify":
        verify_video(args.video_id)


if __name__ == "__main__":
    main()
