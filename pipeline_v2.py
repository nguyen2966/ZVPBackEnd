"""
Pipeline V2: Excel -> YouTube MP4 -> HLS/thumbnail local -> VNDATA S3 -> feed_items_v2.json.

Không import hoặc sửa pipeline Cloudinary cũ. Mọi file tải và output đều dùng hậu tố V2.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from build_url_list import load_urls_from_xlsx
from convert_v2 import convert_one
from vndata_s3 import upload_video_assets, verify_video

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
SOURCE_XLSX = BASE_DIR / "raw" / "batch_1.xlsx"
DOWNLOAD_DIR = BASE_DIR / "downloads_v2"
OUTPUT_FILE = BASE_DIR / "feed_items_v2.json"
LEGACY_FEED_FILE = BASE_DIR / "feed_items.json"
COOKIES_FILE = BASE_DIR / "cookies.txt"
REQUEST_DELAY_RANGE = (3, 7)
MAX_DOWNLOAD_RETRIES = 3
RETRY_BACKOFF_SECONDS = 15

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("youtube_to_vndata_s3")


def download_from_youtube(url: str) -> dict[str, Any]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError('Thiếu yt-dlp; chạy: pip install "yt-dlp[default]"') from exc

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        "format": "bv*[height<=1280]+ba/b[height<=1280]/best",
        "merge_output_format": "mp4",
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "writeinfojson": True,
        "quiet": True,
        "no_warnings": True,
        "js_runtimes": {"node": {}},
    }
    if COOKIES_FILE.exists():
        options["cookiefile"] = str(COOKIES_FILE)
    with yt_dlp.YoutubeDL(options) as downloader:
        return downloader.extract_info(url, download=True)


def download_with_retries(url: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        try:
            return download_from_youtube(url)
        except Exception as exc:  # noqa: BLE001 - retry lỗi mạng/rate-limit từ yt-dlp
            last_error = exc
            if attempt < MAX_DOWNLOAD_RETRIES:
                delay = RETRY_BACKOFF_SECONDS * attempt
                logger.warning("Tải lỗi lần %d/%d; thử lại sau %ds: %s", attempt, MAX_DOWNLOAD_RETRIES, delay, exc)
                time.sleep(delay)
    assert last_error is not None
    raise last_error


def downloaded_mp4(info: dict[str, Any]) -> Path:
    video_id = str(info["id"])
    expected = DOWNLOAD_DIR / f"{video_id}.mp4"
    if expected.exists():
        return expected
    candidates = sorted(DOWNLOAD_DIR.glob(f"{video_id}.*"))
    candidates = [path for path in candidates if path.suffix.lower() == ".mp4"]
    if not candidates:
        raise FileNotFoundError(f"Không tìm thấy MP4 sau khi tải video {video_id}")
    return candidates[0]


def video_id_from_source_url(url: str) -> str | None:
    match = re.search(r"(?:shorts/|[?&]v=)([^/?&]+)", url)
    return match.group(1) if match else None


def video_id_from_feed_item(item: dict[str, Any]) -> str | None:
    url = item.get("video", {}).get("playbackAsset", {}).get("url", "")
    match = re.search(r"/hls/([^/]+)/master\.m3u8(?:\?.*)?$", url)
    return match.group(1) if match else None


def video_id_from_legacy_feed_item(item: dict[str, Any]) -> str | None:
    url = item.get("video", {}).get("playbackAsset", {}).get("url", "")
    match = re.search(r"/([^/?]+)\.m3u8(?:\?.*)?$", url)
    return match.group(1) if match else None


@lru_cache(maxsize=1)
def load_legacy_feed_by_video_id() -> dict[str, dict[str, Any]]:
    """Dùng metadata V1 làm nguồn chuẩn; V2 chỉ thay URL media sang VNDATA."""
    if not LEGACY_FEED_FILE.exists():
        return {}
    items = json.loads(LEGACY_FEED_FILE.read_text(encoding="utf-8"))
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        video_id = video_id_from_legacy_feed_item(item)
        if video_id:
            if video_id in result:
                raise RuntimeError(f"Trùng video ID trong {LEGACY_FEED_FILE.name}: {video_id}")
            result[video_id] = item
    return result


def category_code(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "UNCATEGORIZED"


def build_feed_item(info: dict[str, Any], record: dict[str, Any], urls: dict[str, str]) -> dict[str, Any]:
    legacy_item = load_legacy_feed_by_video_id().get(str(info["id"]))
    if legacy_item is not None:
        item = deepcopy(legacy_item)
        item["video"]["playbackAsset"]["url"] = urls["hls_url"]
        item["video"]["thumbnailAsset"]["url"] = urls["thumbnail_url"]
        return item

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    uploader = info.get("uploader") or info.get("channel") or "Unknown"
    category_name = str(record.get("category") or "Uncategorized")
    avatar_url = (
        info.get("channel_thumbnail")
        or info.get("uploader_avatar")
        or info.get("thumbnail")
        or ""
    )
    video = {
        "id": str(uuid.uuid4()),
        "user": {
            "id": str(uuid.uuid4()),
            "displayName": uploader,
            "username": uploader,
            "avatarUrl": avatar_url,
        },
        "category": {
            "id": str(uuid.uuid4()),
            "code": category_code(category_name),
            "name": category_name,
        },
        "title": info.get("title") or "",
        "caption": info.get("title") or "",
        "durationMs": int((info.get("duration") or 0) * 1000),
        "width": info.get("width"),
        "height": info.get("height"),
        "status": "READY",
        "version": 1,
        "publishedAt": now,
        "createdAt": now,
        "updatedAt": now,
        "deletedAt": None,
        "playbackAsset": {
            "id": str(uuid.uuid4()),
            "url": urls["hls_url"],
            "status": "READY",
            "createdAt": now,
            "updatedAt": now,
        },
        "thumbnailAsset": {
            "id": str(uuid.uuid4()),
            "url": urls["thumbnail_url"],
            "status": "READY",
            "createdAt": now,
            "updatedAt": now,
        },
        "engagement": {
            "likeCount": info.get("like_count") or 0,
            "dislikeCount": 0,
            "bookmarkCount": 0,
        },
        "viewerState": {
            "isBookmarked": False,
            "bookmarkServerVersion": 1,
            "reaction": "NONE",
            "reactionServerVersion": 1,
        },
    }
    return {"video": video}


def load_feed_items() -> list[dict[str, Any]]:
    if not OUTPUT_FILE.exists():
        return []
    return json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))


def save_feed_items(items: list[dict[str, Any]]) -> None:
    temp = OUTPUT_FILE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(OUTPUT_FILE)


def process_record(record: dict[str, Any], *, force_convert: bool, force_upload: bool) -> dict[str, Any]:
    info = download_with_retries(record["url"])
    source = downloaded_mp4(info)
    ok, note = convert_one(source, force=force_convert)
    if not ok:
        raise RuntimeError(f"Convert thất bại: {note}")
    logger.info("Convert %s: %s", info["id"], note)
    urls = upload_video_assets(str(info["id"]), force=force_upload)
    verify_video(str(info["id"]))
    return build_feed_item(info, record, urls)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline YouTube -> VNDATA S3 V2")
    parser.add_argument("xlsx", nargs="?", type=Path, default=SOURCE_XLSX)
    parser.add_argument("--limit", type=int, help="Chỉ xử lý N video đầu để thử nghiệm")
    parser.add_argument("--force-convert", action="store_true")
    parser.add_argument("--force-upload", action="store_true")
    args = parser.parse_args()

    records = load_urls_from_xlsx(args.xlsx)
    existing = load_feed_items()
    done_ids = {video_id_from_feed_item(item) for item in existing}
    pending = [record for record in records if video_id_from_source_url(record["url"]) not in done_ids]
    if args.limit is not None:
        pending = pending[: max(0, args.limit)]
    if not pending:
        logger.info("Không còn video nào cần xử lý.")
        return

    new_items: list[dict[str, Any]] = []
    for index, record in enumerate(pending, start=1):
        if index > 1:
            time.sleep(random.uniform(*REQUEST_DELAY_RANGE))
        try:
            logger.info("[%d/%d] Xử lý %s", index, len(pending), record["url"])
            item = process_record(record, force_convert=args.force_convert, force_upload=args.force_upload)
            new_items.append(item)
            save_feed_items(existing + new_items)
        except Exception as exc:  # noqa: BLE001 - một video lỗi không dừng cả batch
            logger.exception("Bỏ qua video lỗi %s: %s", record["url"], exc)

    logger.info("Đã thêm %d video vào %s", len(new_items), OUTPUT_FILE.name)


if __name__ == "__main__":
    main()
