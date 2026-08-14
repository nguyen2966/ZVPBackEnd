"""
Pipeline: file URL (.xlsx) -> YouTube -> Cloudinary (HLS) -> feed_items.json

Luồng xử lý:
    1. Đọc danh sách URL (+ category) từ file Excel bằng build_url_list.load_urls_from_xlsx
       (xem docstring file đó để biết định dạng file input, vd: raw/batch_1.xlsx).
    2. Bỏ qua các URL đã có sẵn trong feed_items.json (dựa vào video_id nhúng trong
       playbackAsset.url) - chạy lại file không tải lại các video đã thành công.
    3. Tải video + metadata (title, uploader, view_count, like_count, tags...) bằng yt-dlp.
       Không gọi API phụ đề của YouTube (từng gây lỗi 429 riêng, làm rớt cả video dù bản
       thân video tải được) - "caption" lấy trực tiếp từ title.
    4. Upload video lên Cloudinary, yêu cầu tạo sẵn gói HLS ngay lúc upload (eager transformation)
       thay vì đợi tới request đầu tiên của user thật mới xử lý (tránh độ trễ/423 khi đang transcode).
    5. Suy ra URL .m3u8 (HLS) và URL thumbnail (tự chọn khung hình đẹp) trực tiếp từ public_id,
       không cần upload thumbnail riêng.
    6. Ghi thêm (không ghi đè) các feed_item mới vào feed_items.json - có thể import thẳng
       vào bảng feed_item của backend.

Cài đặt trước khi chạy:
    pip install yt-dlp cloudinary python-dotenv pandas openpyxl

Cách chạy:
    python dowload.py [đường_dẫn_file.xlsx]
    (mặc định: raw/batch_1.xlsx - xem SOURCE_XLSX bên dưới)

Biến môi trường bắt buộc - đặt trong file .env cùng thư mục với script này
(xem file .env.example đi kèm để biết định dạng). KHÔNG hardcode secret vào code
và KHÔNG commit file .env lên git:
    CLOUDINARY_CLOUD_NAME
    CLOUDINARY_API_KEY
    CLOUDINARY_API_SECRET

Lưu ý: app demo nội bộ, không public, nên không cần lọc video theo license Creative Commons -
có thể dùng bất kỳ video YouTube công khai nào. Chỉ tránh: (1) đưa video/demo ra công khai,
(2) dùng cho mục đích thương mại. Tránh dùng TikTok làm nguồn (ToS chặt hơn, không có cơ chế
license tương đương CC cho bên thứ ba).

Nếu gặp lỗi "Sign in to confirm you're not a bot" hoặc "Could not copy Chrome cookie database":
    Lỗi thứ 2 xảy ra vì Chrome/Edge khoá file cookie khi đang mở (kể cả chạy nền trong khay hệ
    thống trên Windows). Thay vì phải đóng hẳn trình duyệt mỗi lần chạy pipeline, script này
    dùng cookies.txt export sẵn 1 lần - xem hướng dẫn tạo file ở COOKIES_FILE bên dưới.
"""



from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import cloudinary
import cloudinary.uploader
import yt_dlp
from dotenv import load_dotenv

from build_url_list import load_urls_from_xlsx

load_dotenv()  # đọc file .env cùng thư mục (nếu có) và nạp vào os.environ

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("yt_to_cloudinary")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

avatarUrls= [
  "https://res.cloudinary.com/dphfbhmyo/image/upload/v1782315960/bruno_kkosfv.webp",
  "https://res.cloudinary.com/dphfbhmyo/image/upload/v1782315934/avatar2_sp4keq.jpg",
  "https://res.cloudinary.com/dphfbhmyo/image/upload/v1782315595/messi_afp8ax.webp",
  "https://res.cloudinary.com/dphfbhmyo/image/upload/v1785985295/Screenshot_20260806_095814_Gallery_vaftfd.jpg",
  "https://res.cloudinary.com/dphfbhmyo/image/upload/v1753255794/main-sample.png",
  "https://res.cloudinary.com/dphfbhmyo/image/upload/v1753255793/cld-sample.jpg"
]


def require_env(key: str) -> str:
    """Đọc biến môi trường bắt buộc; báo lỗi rõ ràng nếu thiếu (thường do quên tạo .env)."""
    value = os.environ.get(key)
    if not value:
        raise SystemExit(
            f"Thiếu biến môi trường '{key}'. Tạo file .env ở cùng thư mục "
            f"(xem .env.example) rồi điền giá trị thật, hoặc export {key}=... trước khi chạy."
        )
    return value


# ---- Cấu hình Cloudinary ----
CLOUD_NAME = require_env("CLOUDINARY_CLOUD_NAME")
cloudinary.config(
    cloud_name=CLOUD_NAME,
    api_key=require_env("CLOUDINARY_API_KEY"),
    api_secret=require_env("CLOUDINARY_API_SECRET"),
    secure=True,
)

# File Excel chứa danh sách URL nguồn (xem build_url_list.py để biết định dạng cột).
# Có thể override bằng cách truyền đường dẫn khác qua argv: `python dowload.py duong_dan.xlsx`.
SOURCE_XLSX = Path("raw/batch_1.xlsx")

# YouTube ngày càng chặn request "giống bot" -> cần cookie phiên đăng nhập thật.
# Dùng file cookies.txt export sẵn (KHÔNG cần đóng trình duyệt mỗi lần chạy - phù hợp
# cho pipeline tự động chạy lặp lại nhiều lần). Cách tạo file:
#   1. Cài extension "Get cookies.txt LOCALLY" (Chrome/Firefox)
#   2. Đăng nhập youtube.com, bấm extension -> Export -> lưu thành cookies.txt cùng thư mục script
# File này chứa phiên đăng nhập của bạn - đã được thêm vào .gitignore, KHÔNG commit lên git.
COOKIES_FILE = "cookies.txt"

# Nghỉ ngẫu nhiên giữa các video (giây) để tránh bắn request dồn dập khiến YouTube
# rate-limit/throttle giữa chừng batch lớn (đã quan sát thực tế: chạy 100 URL liên tục
# không nghỉ khiến tỉ lệ lỗi tăng dần theo thời gian chạy, nhưng các URL lỗi lại tải
# được ngay khi thử lại riêng lẻ - dấu hiệu điển hình của rate limit chứ không phải
# video có vấn đề).
REQUEST_DELAY_RANGE = (3, 7)

# Số lần thử lại khi tải 1 video bị lỗi, và thời gian nghỉ tăng dần giữa các lần thử.
MAX_DOWNLOAD_RETRIES = 3
RETRY_BACKOFF_SECONDS = 15


def download_from_youtube(url: str) -> dict[str, Any]:
    """Tải video + metadata từ YouTube, trả về info dict của yt-dlp."""
    ydl_opts = {
        # bv*+ba: best video-only + best audio-only (không ép ext cụ thể, tránh "format not available"
        #   nếu video không có đúng tổ hợp mp4+m4a). Có 2 tầng fallback phía sau:
        #   b[height<=1280]: nếu không tách được stream, lấy bản ghép sẵn gần đúng resolution
        #   best: nếu vẫn không khớp, lấy bất kỳ format nào có sẵn (đảm bảo luôn tải được).
        "format": "bv*[height<=1280]+ba/b[height<=1280]/best",
        "merge_output_format": "mp4",  # ép output cuối luôn là .mp4 dù nguồn video/audio khác codec
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "writeinfojson": True,
        "quiet": True,
        "no_warnings": True,
        "cookiefile": COOKIES_FILE,
        # Cần JS runtime để giải "n challenge" của YouTube (nếu không, yt-dlp chỉ thấy
        # được storyboard images, không thấy format video/audio thật nào).
        # Yêu cầu: đã cài `pip install "yt-dlp[default]"` (có gói yt-dlp-ejs) và có Node.js >= 22.
        "js_runtimes": {"node": {}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    return info


def upload_to_cloudinary(video_path: Path, public_id: str) -> dict[str, Any]:
    """
    Upload video lên Cloudinary với eager transformation tạo gói HLS (full_hd streaming profile)
    ngay lúc upload, thay vì để user đầu tiên gánh độ trễ transcode.
    """
    return cloudinary.uploader.upload(
        str(video_path),
        resource_type="video",
        public_id=public_id,
        eager=[
            {"streaming_profile": "full_hd", "format": "m3u8"},
        ],
        eager_async=True,
        overwrite=True,
    )


def build_urls(public_id: str) -> dict[str, str]:
    """
    Suy ra URL HLS và URL thumbnail trực tiếp từ public_id, không cần lưu riêng:
      - sp_full_hd + .m3u8  -> gói HLS adaptive bitrate
      - so_auto   + .jpg    -> Cloudinary tự chọn khung hình đẹp làm thumbnail
    Đổi "sp_full_hd" thành "sp_auto" nếu muốn Cloudinary tự chọn profile thay vì cố định full_hd.
    """
    base = f"https://res.cloudinary.com/{CLOUD_NAME}/video/upload"
    return {
        "hls_url": f"{base}/sp_auto/{public_id}.m3u8",
        "thumbnail_url": f"{base}/so_auto/{public_id}.jpg",
    }


def category_code(name: str) -> str:
    """Suy ra 'code' dạng SCREAMING_SNAKE_CASE từ tên category, vd: 'Fun and Memes' -> 'FUN_AND_MEMES'."""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "UNCATEGORIZED"


def build_video_item(
    info: dict[str, Any], record: dict[str, Any], urls_out: dict[str, str]
) -> dict[str, Any]:
    """
    Map dữ liệu từ YouTube (info) + Excel (record) + Cloudinary (urls_out) sang đúng schema
    "video" của backend. Những trường không có nguồn thật (id nội bộ, engagement không public
    trên YouTube như dislike/bookmark, version, viewerState...) được sinh uuid4/ngẫu nhiên.
    """

    def iso(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    created_at = datetime.now(timezone.utc)
    published_at = created_at + timedelta(hours=random.randint(0, 48))
    updated_at = published_at

    uploader = info.get("uploader") or "Unknown"
    category_name = record.get("category") or "Uncategorized"

    video = {
        "id": str(uuid.uuid4()),
        "user": {
            "id": str(uuid.uuid4()),
            "displayName": uploader,
            "username": uploader,
            "avatarUrl": random.choice(avatarUrls),
        },
        "category": {
            "id": str(uuid.uuid4()),
            "code": category_code(category_name),
            "name": category_name,
        },
        "title": info.get("title") or "",
        "caption": info.get("title") or "",  # không gọi API phụ đề của YouTube nữa -> dùng title
        "durationMs": int((info.get("duration") or 0) * 1000),
        "width": info.get("width"),
        "height": info.get("height"),
        "status": "READY",
        "version": random.randint(1, 6),
        "publishedAt": iso(published_at),
        "createdAt": iso(created_at),
        "updatedAt": iso(updated_at),
        "deletedAt": None,
        "playbackAsset": {
            "id": str(uuid.uuid4()),
            "url": urls_out["hls_url"],
            "status": "READY",
            "createdAt": iso(created_at),
            "updatedAt": iso(updated_at),
        },
        "thumbnailAsset": {
            "id": str(uuid.uuid4()),
            "url": urls_out["thumbnail_url"],
            "status": "READY",
            "createdAt": iso(created_at),
            "updatedAt": iso(updated_at),
        },
        "engagement": {
            "likeCount": info.get("like_count") or 0,   # số liệu thật từ YouTube
            "dislikeCount": random.randint(0, 300),      # YouTube không public dislike -> random
            "bookmarkCount": random.randint(0, 1000),    # không có khái niệm này trên YouTube -> random
        },
        "viewerState": {
            "isBookmarked": random.choice([True, False]),
            "bookmarkServerVersion": random.randint(1, 6),
            "reaction": random.choice(["LIKE", "DISLIKE", "NONE"]),
            "reactionServerVersion": random.randint(1, 6),
        },
    }
    return {"video": video}


def download_with_retries(url: str) -> dict[str, Any]:
    """
    Gọi download_from_youtube, thử lại tối đa MAX_DOWNLOAD_RETRIES lần nếu lỗi (nghỉ tăng
    dần giữa các lần) - hầu hết lỗi giữa chừng batch lớn là do YouTube rate-limit tạm thời,
    không phải video hỏng, nên thử lại sau vài giây thường sẽ qua.
    """
    last_exc: Exception | None = None
    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        try:
            return download_from_youtube(url)
        except Exception as exc:  # noqa: BLE001 - muốn bắt mọi lỗi từ yt-dlp để thử lại
            last_exc = exc
            if attempt < MAX_DOWNLOAD_RETRIES:
                backoff = RETRY_BACKOFF_SECONDS * attempt
                logger.warning(
                    "Lỗi tải %s (lần %d/%d): %s - thử lại sau %ds",
                    url, attempt, MAX_DOWNLOAD_RETRIES, exc, backoff,
                )
                time.sleep(backoff)
    raise last_exc


def video_id_from_url(url: str) -> str | None:
    """Lấy YouTube video id từ URL (hỗ trợ cả dạng /shorts/ID và ?v=ID)."""
    m = re.search(r"(?:shorts/|[?&]v=)([^/?&]+)", url)
    return m.group(1) if m else None


def video_id_from_feed_item(item: dict[str, Any]) -> str | None:
    """Lấy lại YouTube video id từ 1 feed_item đã ghi (nhúng trong playbackAsset.url)."""
    m = re.search(r"/([^/]+)\.m3u8$", item["video"]["playbackAsset"]["url"])
    return m.group(1) if m else None


def load_existing_feed_items(path: Path) -> list[dict[str, Any]]:
    """Đọc feed_items.json hiện có (nếu có) để biết video nào đã xử lý thành công trước đó."""
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def run_pipeline(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    feed_items: list[dict[str, Any]] = []

    for idx, record in enumerate(records, start=1):
        url = record["url"]
        try:
            if idx > 1:
                time.sleep(random.uniform(*REQUEST_DELAY_RANGE))

            logger.info("[%d/%d] Đang tải: %s", idx, len(records), url)
            info = download_with_retries(url)
            video_id = info["id"]
            video_path = DOWNLOAD_DIR / f"{video_id}.{info['ext']}"

            logger.info("Đang upload lên Cloudinary: %s", video_id)
            upload_to_cloudinary(video_path, public_id=video_id)

            urls_out = build_urls(video_id)

            feed_items.append(build_video_item(info, record, urls_out))
            logger.info("Xong: %s -> %s", video_id, urls_out["hls_url"])

        except Exception as exc:  # noqa: BLE001 - pipeline cần chạy tiếp dù 1 video lỗi
            logger.error("Lỗi xử lý %s: %s", url, exc)
            continue

    return feed_items


if __name__ == "__main__":
    xlsx_path = Path(sys.argv[1]) if len(sys.argv) > 1 else SOURCE_XLSX
    if not xlsx_path.exists():
        raise SystemExit(
            f"Không tìm thấy file input: {xlsx_path}. Truyền đường dẫn khác qua "
            f"`python dowload.py duong_dan.xlsx` hoặc sửa SOURCE_XLSX trong file."
        )

    source_records = load_urls_from_xlsx(xlsx_path)
    if not source_records:
        logger.error("File %s không có URL nào để xử lý.", xlsx_path)
        raise SystemExit(1)

    output_path = Path("feed_items.json")
    existing_items = load_existing_feed_items(output_path)
    done_ids = {video_id_from_feed_item(item) for item in existing_items}

    pending_records = [r for r in source_records if video_id_from_url(r["url"]) not in done_ids]
    skipped = len(source_records) - len(pending_records)
    if skipped:
        logger.info(
            "Bỏ qua %d URL đã có sẵn trong %s (không tải lại).", skipped, output_path
        )
    if not pending_records:
        logger.info("Không còn URL nào cần xử lý.")
        raise SystemExit(0)

    new_items = run_pipeline(pending_records)

    all_items = existing_items + new_items
    output_path.write_text(json.dumps(all_items, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "Đã thêm %d feed_item mới (tổng %d) vào %s",
        len(new_items), len(all_items), output_path,
    )