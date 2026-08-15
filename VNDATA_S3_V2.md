# Pipeline VNDATA S3 V2

Pipeline V2 không thay thế hành vi của `convert.py`, `dowload.py`, `feed_items.json`,
`downloads/`, `public/` hoặc các endpoint V1. Route V2 chỉ được thêm vào app FastAPI hiện có.

## Thành phần

- `convert_v2.py`: tạo HLS multivariant và thumbnail trong `public_v2/`.
- `vndata_s3.py`: kiểm tra kết nối, upload và verify Range GET trên VNDATA S3.
- `pipeline_v2.py`: tải YouTube, convert, upload, verify rồi ghi `feed_items_v2.json`.
- `main.py`: app duy nhất cần chạy; route `/api/v2/feed` chỉ trả JSON và các endpoint V1 vẫn được giữ nguyên.

## Cài dependency

```powershell
py -3.12 -m pip install -r requirements-v2.txt
```

FFmpeg, ffprobe và Node.js cần có trong PATH.

## Kiểm tra bucket

```powershell
py -3.12 vndata_s3.py check
```

Lệnh này chỉ `HEAD` bucket, không upload hoặc thay đổi bucket policy.

## Bucket policy

Để URL trong feed đọc được trực tiếp, bucket cần cho phép public `s3:GetObject` nhưng không
cho public PUT, DELETE hoặc LIST:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadVideoAssets",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::zvideo-media/*"
    }
  ]
}
```

## Chạy thử một video

```powershell
py -3.12 pipeline_v2.py --limit 1
```

Pipeline chỉ thêm feed item sau khi:

1. tải MP4 thành công;
2. tạo đủ HLS và thumbnail;
3. upload đủ object, master playlist được upload cuối;
4. master có đúng MIME type;
5. Range GET một `.mp4dv` trả HTTP 206 và `Content-Range`.

Output mới:

```text
downloads_v2/<video_id>.mp4
public_v2/hls/<video_id>/master.m3u8
public_v2/hls/<video_id>/<tier>/index.m3u8
public_v2/hls/<video_id>/<tier>/<video_id>.mp4dv
public_v2/thumbs/<video_id>.jpg
feed_items_v2.json
```

## Chạy backend thống nhất

```powershell
py -3.12 main.py
```

V2 API: `http://localhost:3000/api/v2/feed`.

Endpoint V1 cũ vẫn là `http://localhost:3000/api/feed`. V2 không có route media/proxy;
HLS và thumbnail trong response đều là URL VNDATA S3.

## Lệnh uploader riêng

```powershell
py -3.12 vndata_s3.py upload VIDEO_ID
py -3.12 vndata_s3.py verify VIDEO_ID
```

Dùng `--force` với lệnh upload chỉ khi muốn ghi lại object cùng key. Mặc định object có cùng
kích thước và metadata sẽ được bỏ qua.
