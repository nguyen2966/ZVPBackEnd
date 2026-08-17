# convert_v2.py
"""
Chuẩn bị asset HLS V2 để upload lên VNDATA S3, theo đúng định dạng client Android
(ZVideoPlus) đang mong đợi - xem des.md. Script này không sửa output của convert.py.

Với mỗi mp4 trong INPUT_DIR, script tạo ra:
    public_v2/hls/<video_id>/master.m3u8             -> master playlist MULTIVARIANT
    public_v2/hls/<video_id>/<tier>/index.m3u8       -> media playlist từng rendition
    public_v2/hls/<video_id>/<tier>/<video_id>.mp4dv -> fMP4 byte-range
    public_v2/thumbnails/<video_id>.jpg              -> frame giải mã đầu tiên của video

Bám theo des.md:
    §1 BẮT BUỘC  master multivariant, mỗi rendition khai báo BANDWIDTH/RESOLUTION/CODECS
    §2 BẮT BUỘC  mọi media playlist có #EXT-X-PLAYLIST-TYPE:VOD và #EXT-X-ENDLIST
                 (ffmpeg tự thêm khi dùng -hls_playlist_type vod)
    §4 TỐI ƯU    đóng gói byte-range fMP4 1 file/rendition (#EXT-X-MAP + #EXT-X-BYTERANGE),
                 giống Cloudinary, để client preload đúng đoạn thay vì tải trọn segment

Vì sao phải ép GOP (-g):
    hls_time chỉ cắt được tại keyframe. Nguồn từ YouTube có keyframe cách nhau ~8.3s nên
    dù đặt -hls_time 4 vẫn ra segment 8.3s (segment đầu ~4MB). Ép GOP = fps × 4 cho ra
    đúng TARGETDURATION:4 và segment đầu ~600KB - đúng thứ client warm khi preload (des.md §4).

URI trong master được giữ ở dạng tương đối ("pg_5/index.m3u8"). Nhờ vậy playlist hoạt
động với endpoint path-style có tên bucket, custom domain hoặc CDN mà không phải sửa lại.

Cài đặt trước khi chạy:
    ffmpeg + ffprobe phải có trong PATH.

Cách chạy:
    python convert_v2.py [thư_mục_input] [--force]
    (mặc định: downloads_v2; --force chỉ tạo lại asset trong public_v2)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "downloads_v2"
HLS_DIR = BASE_DIR / "public_v2" / "hls"
THUMB_DIR = BASE_DIR / "public_v2" / "thumbnails"

# Độ dài segment mục tiêu (giây). Cloudinary dùng 4s - xem des.md §4.
SEGMENT_SECONDS = 4


class Tier(NamedTuple):
    """1 nấc trong ABR ladder. Ladder tham chiếu lấy từ des.md §1 (đo thật từ Cloudinary)."""
    name: str
    height: int
    bitrate_k: int
    maxrate_k: int
    bufsize_k: int


# Ladder xếp từ chất lượng cao xuống thấp.
LADDER: list[Tier] = [
    Tier("pg_5", 1280, 3440, 3680, 5160),
    Tier("pg_4", 960, 1640, 1755, 2460),
    Tier("pg_3", 640, 640, 685, 960),
    Tier("pg_2", 480, 410, 440, 615),
    Tier("pg_1", 320, 280, 300, 420),
]

# des.md §1 yêu cầu tối thiểu 3 rendition.
MIN_TIERS = 3


def run(cmd: list[str]) -> tuple[bool, str]:
    """Chạy 1 lệnh, trả về (thành công, stderr)."""
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode == 0, (proc.stderr or "").strip()


def probe_source(src: Path) -> tuple[int, int, float]:
    """Đọc width/height/fps của video nguồn bằng ffprobe."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-of", "json", str(src),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    stream = json.loads(proc.stdout)["streams"][0]
    num, _, den = stream["r_frame_rate"].partition("/")
    fps = float(num) / float(den or 1)
    return int(stream["width"]), int(stream["height"]), fps


def tiers_for(source_height: int) -> list[Tier]:
    """
    Chọn các rendition <= chiều cao nguồn (không upscale vì chỉ tốn dung lượng, không thêm nét).
    Nếu nguồn quá nhỏ khiến không đủ MIN_TIERS thì lấy các nấc thấp nhất để vẫn đủ số lượng
    des.md §1 yêu cầu.
    """
    tiers = [t for t in LADDER if t.height <= source_height]
    if len(tiers) < MIN_TIERS:
        tiers = LADDER[-MIN_TIERS:]
    return tiers


def build_cmd(src: Path, out_dir: Path, video_id: str, tiers: list[Tier], fps: float, use_gpu: bool) -> list[str]:
    """Dựng lệnh ffmpeg tạo toàn bộ ABR ladder trong 1 lần chạy (1 lần decode, N lần encode)."""
    n = len(tiers)
    gop = max(1, round(fps * SEGMENT_SECONDS))

    # [0:v]split=N[s0]...[sN-1]; [s0]scale=-2:H0[v0]; ...   (scale=-2 giữ đúng tỉ lệ, ép width chẵn)
    splits = "".join(f"[s{i}]" for i in range(n))
    scales = ";".join(f"[s{i}]scale=-2:{t.height}[v{i}]" for i, t in enumerate(tiers))
    filter_complex = f"[0:v]split={n}{splits};{scales}"

    cmd: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if use_gpu:
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(src), "-filter_complex", filter_complex]

    for i in range(n):
        cmd += ["-map", f"[v{i}]", "-map", "0:a"]

    if use_gpu:
        cmd += ["-c:v", "h264_nvenc", "-preset", "p4", "-forced-idr", "1"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-sc_threshold", "0"]
    # ép keyframe đều đặn để hls_time cắt được đúng SEGMENT_SECONDS
    cmd += ["-g", str(gop), "-keyint_min", str(gop)]

    for i, t in enumerate(tiers):
        cmd += [
            f"-b:v:{i}", f"{t.bitrate_k}k",
            f"-maxrate:v:{i}", f"{t.maxrate_k}k",
            f"-bufsize:v:{i}", f"{t.bufsize_k}k",
        ]

    var_map = " ".join(f"v:{i},a:{i},name:{t.name}" for i, t in enumerate(tiers))
    cmd += [
        "-c:a", "aac", "-b:a", "128k",
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_playlist_type", "vod",              # -> tự sinh #EXT-X-ENDLIST (des.md §2)
        "-hls_segment_type", "fmp4",              # -> fMP4 thay vì MPEG-TS (des.md §4)
        "-hls_flags", "single_file+independent_segments",  # -> 1 file + #EXT-X-BYTERANGE
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", var_map,
        "-hls_segment_filename", str(out_dir / "%v" / f"{video_id}.mp4dv"),
        str(out_dir / "%v" / "index.m3u8"),
    ]
    return cmd


def normalize_master(master: Path) -> None:
    """
    Giữ URI variant tương đối và chuẩn hoá dấu phân cách Windows sang dấu "/".
    Không dùng absolute path vì URL S3 path-style còn chứa tên bucket.
    """
    lines = master.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for line in lines:
        # trên Windows ffmpeg ghi URI bằng dấu "\" -> chuẩn hoá về "/" trước khi so khớp
        stripped = line.strip().replace("\\", "/")
        if stripped.endswith("/index.m3u8"):
            out.append(stripped)
        elif stripped:
            out.append(line)
    master.write_text("\n".join(out) + "\n", encoding="utf-8")


def convert_one(src: Path, force: bool) -> tuple[bool, str]:
    """Tạo ABR ladder + thumbnail cho 1 file mp4."""
    HLS_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    video_id = src.stem
    out_dir = HLS_DIR / video_id
    master = out_dir / "master.m3u8"
    thumb = THUMB_DIR / f"{video_id}.jpg"
    notes: list[str] = []

    # Layout cũ (single-rendition .ts) không còn dùng -> xoá để tạo lại theo layout mới.
    if (out_dir / "index.m3u8").exists():
        shutil.rmtree(out_dir, ignore_errors=True)
        notes.append("đã xoá layout .ts cũ")

    if force and out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    if force and thumb.exists():
        thumb.unlink()

    if master.exists():
        notes.append("HLS đã có")
    else:
        try:
            _, height, fps = probe_source(src)
        except Exception as exc:  # noqa: BLE001 - file hỏng thì bỏ qua, chạy tiếp video khác
            return False, f"ffprobe lỗi: {exc}"

        tiers = tiers_for(height)
        for tier in tiers:
            (out_dir / tier.name).mkdir(parents=True, exist_ok=True)

        ok, err = run(build_cmd(src, out_dir, video_id, tiers, fps, use_gpu=True))
        if not ok:
            ok, err = run(build_cmd(src, out_dir, video_id, tiers, fps, use_gpu=False))
            notes.append(f"{len(tiers)} tier (CPU)" if ok else "HLS lỗi")
        else:
            notes.append(f"{len(tiers)} tier (GPU)")
        if not ok:
            return False, f"{'; '.join(notes)}: {err.splitlines()[-1] if err else 'không rõ lỗi'}"

        normalize_master(master)

    # Không seek: ffmpeg xuất đúng frame video giải mã đầu tiên, không phải frame ở 1,5 giây.
    if thumb.exists():
        notes.append("thumb đã có")
    else:
        ok, err = run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src), "-map", "0:v:0", "-frames:v", "1", "-q:v", "2", "-an", str(thumb),
        ])
        if not ok:
            return False, f"thumbnail lỗi: {err.splitlines()[-1] if err else 'không rõ lỗi'}"
        notes.append("thumb")

    return True, "; ".join(notes)


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv
    input_dir = Path(args[0]) if args else INPUT_DIR

    if not input_dir.exists():
        raise SystemExit(f"Không tìm thấy thư mục input: {input_dir}")

    HLS_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)

    sources = sorted(input_dir.glob("*.mp4"))
    if not sources:
        raise SystemExit(f"Không có file .mp4 nào trong {input_dir}")

    print(f"Tìm thấy {len(sources)} video trong {input_dir}. Tạo ABR ladder + thumbnail...")
    failed: list[str] = []

    for idx, src in enumerate(sources, start=1):
        ok, note = convert_one(src, force)
        print(f"[{idx}/{len(sources)}] {'OK ' if ok else 'LỖI'} {src.name} - {note}")
        if not ok:
            failed.append(src.name)

    print(f"\nHoàn tất: {len(sources) - len(failed)}/{len(sources)} video.")
    print(f"  Master    -> {HLS_DIR}/<video_id>/master.m3u8")
    print(f"  Rendition -> {HLS_DIR}/<video_id>/<tier>/index.m3u8 + <video_id>.mp4dv")
    print(f"  Thumbnail -> {THUMB_DIR}/<video_id>.jpg")
    if failed:
        print(f"  Thất bại ({len(failed)}): {', '.join(failed)}")


if __name__ == "__main__":
    main()
