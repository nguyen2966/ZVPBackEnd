# Yêu cầu định dạng HLS & Feed API cho backend


Tài liệu này mô tả định dạng mà client Android (ZVideoPlus) đang được xây dựng để tiêu thụ, dựa trên
Cloudinary `sp_auto` — nền tảng đã dùng trong suốt quá trình phát triển và tối ưu.
 
Mỗi mục có mức độ: **BẮT BUỘC** / **NÊN CÓ** / **TỐI ƯU**.


---


## 0. Tóm tắt: khác biệt hiện tại


Backend hiện tại trả về HLS single-rendition với segment `.ts` rời. Cloudinary trả về multivariant
với segment đóng gói byte-range trong một file fMP4.


| | Cloudinary (client được thiết kế theo) | Backend hiện tại |
|---|---|---|
| Master playlist | multivariant, 3–5 rendition | **không có** — chỉ một media playlist |
| Đóng gói segment | 1 file `.mp4dv` + `#EXT-X-BYTERANGE` | nhiều file `.ts` rời |
| Số playlist / item | 2 (master + variant) | 1 |
| ABR | có | không |


Hệ quả: toàn bộ phần chọn chất lượng theo băng thông, bitrate pin, và preload chính xác theo byte
range của client đang **không hoạt động** — không phải lỗi, nhưng là code chết.


---


## 1. BẮT BUỘC — Master playlist phải là multivariant


### Hiện trạng
Client resolve 29/29 item đều nhận `bitrate=null`, tức playlist trả về là `HlsMediaPlaylist` chứ
không phải `HlsMultivariantPlaylist`.


### Yêu cầu
Endpoint chính của mỗi video phải trả về master playlist liệt kê **tối thiểu 3 rendition**:


```
#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=3440000,RESOLUTION=720x1280,FRAME-RATE=30.000,CODECS="avc1.640028,mp4a.40.2"
/video/upload/sp_auto/pg_5/v1786417061/<publicId>.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1640000,RESOLUTION=540x960,FRAME-RATE=30.000,CODECS="avc1.640028,mp4a.40.2"
/video/upload/sp_auto/pg_4/v1786417061/<publicId>.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=640000,RESOLUTION=360x640,FRAME-RATE=30.000,CODECS="avc1.640028,mp4a.40.2"
/video/upload/sp_auto/pg_3/v1786417061/<publicId>.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=410000,RESOLUTION=270x480,FRAME-RATE=30.000,CODECS="avc1.640028,mp4a.40.2"
/video/upload/sp_auto/pg_2/v1786417061/<publicId>.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=280000,RESOLUTION=180x320,FRAME-RATE=30.000,CODECS="avc1.640028,mp4a.40.2"
/video/upload/sp_auto/pg_1/v1786417061/<publicId>.m3u8
```


Bắt buộc từng dòng:


- `BANDWIDTH` — **phải có**. Client dùng chính giá trị này để chọn rendition:
 `bitrate cao nhất ≤ bandwidthEstimate × 0.8`. Thiếu nó thì không chọn được gì.
- `RESOLUTION`, `CODECS` — nên có, giúp Media3 loại sớm rendition mà thiết bị không giải mã được.
- URL variant có thể là **absolute path** (bắt đầu `/`) hoặc tên file tương đối. Client dùng
 `UriUtil.resolve` nên cả hai đều được.


### Vì sao bắt buộc
Không có multivariant thì mất cả ba thứ:
- Adaptive bitrate — người mạng yếu không thể tự tụt xuống tier thấp, dẫn tới **stall thay vì giảm
 chất lượng**. Đây là lỗi client đã từng gặp và sửa (p90 từng vọt lên 3015ms vì bị chặn đường tụt tier).
- Bitrate pin — client warm sẵn đúng một rendition rồi cap trần playback vào đó để bảo đảm cache hit.
- Preload theo băng thông — hiện chọn rendition theo `bandwidthEstimate` đo thời gian thực.


### Ladder đề nghị
Client đã chạy tốt với ladder của Cloudinary. Tham chiếu (đo thật):


| Tier | BANDWIDTH | RESOLUTION |
|---|---|---|
| pg_5 | 3 440 000 | 720×1280 |
| pg_4 | 1 640 000 | 540×960 |
| pg_3 | 640 000 | 360×640 |
| pg_2 | 410 000 | 270×480 |
| pg_1 | 280 000 | 180×320 |


Ladder **không cần giống nhau giữa các video** — Cloudinary cũng khác nhau (3 tier tới 5 tier, top từ
360p tới 1080p) và client xử lý được. Nhưng **nên ổn định theo thời gian** cho cùng một video: client
cache playlist trong RAM với TTL 60s, và Cloudinary từng đổi ladder giữa các lần request khiến client
phải thêm TTL để phòng.


---


## 2. BẮT BUỘC — Mọi media playlist phải kết thúc bằng `#EXT-X-ENDLIST`


### Yêu cầu
Mỗi media playlist (từng rendition) phải có:


```
#EXT-X-PLAYLIST-TYPE:VOD
...
#EXT-X-ENDLIST
```


### Vì sao bắt buộc — đây là lỗi tốn nhiều thời gian nhất


Thiếu `#EXT-X-ENDLIST`, Media3 coi playlist là **LIVE stream**, liên tục poll chờ segment mới, và sau
`targetDuration × 3.5` sẽ ném `HlsPlaylistTracker$PlaylistStuckException` — video **hỏng hẳn**, không
phát được.


Trích source Media3 1.10.1 (`DefaultHlsPlaylistTracker`):
```java
} else if (!playlistSnapshot.hasEndTag) {          // CHỈ khi thiếu ENDLIST
   ...
} else if (currentTimeMs - lastSnapshotChangeMs
       > usToMs(targetDurationUs) * playlistStuckTargetDurationCoefficient) {
   playlistError = new PlaylistStuckException(playlistUrl);
}
DEFAULT_PLAYLIST_STUCK_TARGET_DURATION_COEFFICIENT = 3.5
```


Với `TARGETDURATION:4` thì đúng **14 giây**. Đo trên thiết bị: first frame `13:52:51.647` → error
`13:53:05.638` = **13.99s**. Khớp tuyệt đối.


Cloudinary đáp ứng 100%: quét toàn bộ **250 variant** (50 video × 5 tier) đều có `ENDLIST=1`,
`PLAYLIST-TYPE=VOD`.


---


## 3. NÊN CÓ — Segment phải hỗ trợ HTTP Range và trả `206`


### Yêu cầu
Request có header `Range` lên file segment phải trả **`206 Partial Content`** kèm `Content-Range`:


```
GET /.../<publicId>.mp4dv
Range: bytes=1256-617267


HTTP/2 206
Content-Range: bytes 1256-617267/3978541
```


### Vì sao
Client preload bằng `CacheWriter` với `DataSpec` chỉ định `position` + `length` chính xác. Không có
Range, nó buộc phải tải **toàn bộ** file segment thay vì đúng đoạn cần.


Cloudinary tôn trọng Range trên segment — đã xác nhận trên wire: `Range: bytes=3899392-` → `206` +
`Content-Range`.


### Cảnh báo quan trọng: đừng "hỗ trợ nửa vời" trên playlist


Cloudinary **quảng cáo `accept-ranges: bytes` nhưng phớt lờ Range trên `.m3u8`**:


```
Request:  Range: bytes=200-
Response: HTTP/2 200          ← không phải 206
         content-length: 504  ← trả full body từ offset 0
```


Điều này từng làm hỏng cache đĩa của client: một playlist tải dở bị cắt cụt, lần sau client xin phần
còn lại bằng Range, server trả full body từ offset 0, client ghép byte sai vị trí → mất dòng
`#EXT-X-ENDLIST` cuối file → rơi vào đúng lỗi ở mục 2.


**Yêu cầu**: hoặc hỗ trợ Range đúng chuẩn (trả `206`), hoặc **không quảng cáo `accept-ranges`** trên
playlist. Đừng làm nửa vời. Client hiện đã tự phòng bằng cách không cache playlist xuống đĩa, nhưng
đây là hành vi HTTP sai và nên sửa ở nguồn.


---


## 4. TỐI ƯU — Đóng gói segment kiểu byte-range (fMP4 một file)


Không bắt buộc: Media3 xử lý cả `.ts` rời và byte-range fMP4. Nhưng byte-range cho client preload
chính xác hơn.


### Định dạng Cloudinary
```
#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-MAP:URI="<publicId>.mp4dv",BYTERANGE="1256@0"
#EXTINF:4.066667,
#EXT-X-BYTERANGE:616012@1256
<publicId>.mp4dv
#EXTINF:4.0,
#EXT-X-BYTERANGE:573453@617268
<publicId>.mp4dv
...
#EXT-X-ENDLIST
```


- **Một file `.mp4dv` cho mỗi rendition**, cắt bằng `#EXT-X-BYTERANGE:length@offset`
- fMP4 chứ không phải MPEG-TS: `#EXT-X-VERSION:7` + `#EXT-X-MAP` làm init segment
- Init segment nhỏ: `BYTERANGE="1256@0"`
- Tên segment **tương đối**, resolve theo baseUri của media playlist
- `TARGETDURATION:4`, mỗi segment ~4s, ~120KB–620KB tùy tier


### Lợi ích cụ thể
Client chỉ warm **init segment + segment đầu tiên** (~617KB) để đủ vượt ngưỡng
`bufferForPlaybackMs`. Với `.ts` rời, `byteRangeLength` là `C.LENGTH_UNSET` nên `CacheWriter` phải tải
**trọn file segment** — nặng hơn dự kiến mà không thêm lợi ích.


Ngoài ra một file/rendition giảm số connection, liên quan trực tiếp tới mục 6.


---


## 5. BẮT BUỘC — Feed API JSON


Client đọc **đúng** các field dưới đây. Field khác được bỏ qua an toàn (`ignoreUnknownKeys = true`).


```json
{
 "items": [
   {
     "position": 0,
     "video": {
       "id": "96985e10-2bd9-46c9-b309-bc3caf7c28dc",
       "user": {
         "id": "776b137b-...",
         "displayName": "LAPTOP AZ",
         "username": "laptopaz",
         "avatarUrl": "https://.../avatar.jpg"
       },
       "category": { "name": "Technology" },
       "title": "...",
       "caption": "...",
       "durationMs": 77000,
       "playbackAsset": { "url": "https://.../<publicId>.m3u8" },
       "thumbnailAsset": { "url": "https://.../thumb.jpg" },
       "engagement": { "likeCount": 3569, "dislikeCount": 200, "bookmarkCount": 205 },
       "viewerState": { "isBookmarked": false, "reaction": "DISLIKE" }
     }
   }
 ]
}
```


### Quy tắc client áp dụng khi dữ liệu thiếu/sai


| Field | Thiếu hoặc rỗng | Hệ quả |
|---|---|---|
| `video.id` | rỗng | **Loại bỏ cả item** |
| `playbackAsset.url` | rỗng/thiếu | **Loại bỏ cả item** |
| `user.username` | rỗng | fallback sang `displayName`, rồi sang `"unknown"` |
| `user.displayName` | rỗng | fallback sang `username`, rồi `"Unknown creator"` |
| `category.name` | rỗng | `"Uncategorized"` |
| `avatarUrl` / `thumbnailAsset.url` | rỗng | UI dùng drawable placeholder |
| `durationMs`, các `*Count` | âm | clamp về 0 |
| `*Count` | là string `""` | coerce về 0 |


Client **chịu lỗi tốt**: một item sai sẽ bị loại riêng, không làm sập cả trang. Nhưng hai field
`id` và `playbackAsset.url` là bắt buộc — thiếu là item biến mất khỏi feed.


### Hai điều **không được** làm


**Đừng lồng thêm tầng.** Đã từng xảy ra: API trả `items[].video.video.{...}` thay vì
`items[].video.{...}`. Vì client bỏ qua unknown key, nó **không báo lỗi gì** — chỉ nhận DTO toàn giá
trị mặc định rồi loại sạch mọi item, và **feed rỗng hoàn toàn mà không có một dòng lỗi nào**. Đây là
kiểu bug tệ nhất để truy.


**Đừng đổi kiểu.** `likeCount: ""` (string rỗng thay vì số) từng làm `kotlinx.serialization` ném
`MissingFieldException` và **abort toàn bộ response** — mất cả 10 item vì 1 item sai. Client đã tự
phòng, nhưng đúng kiểu vẫn tốt hơn.


---


## 6. CẦN XÁC NHẬN — Năng lực connection của hạ tầng


Chưa kết luận được, cần backend xác nhận.


### Hiện trạng đo được
Session 3 phút 16 giây trên backend mới:


```
javax.net.ssl.SSLHandshakeException: connection closed   × 323
 ├─ player load error: MANIFEST 40, SEGMENT 28  (67/68 là SSL)
 └─ preload stopped: 204/315
success rate: 37%  (33/90 item phát được)
```


Bursty tới **16 lỗi/giây**. Tần suất request từ client:


```
preload network attempts: 404 request / 3.3 phút = 123 request/phút
 (chưa tính traffic của 2 player)
peak đồng thời: 13 request trong 300ms sau mỗi lần scroll settle
```


`connection closed` giữa lúc TLS handshake là dấu hiệu phía server/tunnel chủ động đóng — thường do
giới hạn rate hoặc số connection đồng thời.


### Cần xác nhận
1. Tunnel đang dùng **ngrok free tier**? Nếu có, giới hạn rate của free tier rất có thể là nguyên nhân
  và cần nâng plan hoặc deploy thật.
2. Server có bật **HTTP keep-alive**? Nếu mỗi request phải TLS handshake lại (đo được 0.33s đơn lẻ,
  1.29s khi song song) thì đó là chi phí rất lớn.
3. Có giới hạn connection đồng thời per-IP ở tầng server/proxy?


### Phía client cũng phải sửa
Không đẩy hết cho backend. Client hiện **không có** giới hạn concurrency cho preload và **không có
backoff** cho URL đã fail — dẫn tới `315 lần fail trên chỉ 50 URL = 6.3 lần/URL`, tức retry mù và tự
khuếch đại tải. Hai việc này thuộc phía client và sẽ được sửa.


---


## 7. Tiêu chí nghiệm thu


Backend tự kiểm bằng các lệnh sau. Đặt `MASTER`, `VARIANT`, `SEGMENT` theo URL thật.


```bash
# 1. Master phải là multivariant, tối thiểu 3 rendition
curl -s "$MASTER" | grep -c '#EXT-X-STREAM-INF'
# kỳ vọng: >= 3


# 2. Mọi rendition phải khai báo BANDWIDTH
curl -s "$MASTER" | grep '#EXT-X-STREAM-INF' | grep -c 'BANDWIDTH='
# kỳ vọng: bằng số rendition ở bước 1


# 3. Mọi media playlist phải kết thúc VOD  ← quan trọng nhất
for V in $(curl -s "$MASTER" | grep -v '^#'); do
 curl -s "$(dirname $MASTER)/$V" | grep -c '#EXT-X-ENDLIST'
done
# kỳ vọng: tất cả = 1


curl -s "$VARIANT" | grep '#EXT-X-PLAYLIST-TYPE'
# kỳ vọng: #EXT-X-PLAYLIST-TYPE:VOD


# 4. Segment phải trả 206 cho Range
curl -s -o /dev/null -D - -H "Range: bytes=100-200" "$SEGMENT" \
 | grep -iE '^HTTP/|content-range'
# kỳ vọng: 206 + Content-Range: bytes 100-200/<total>


# 5. Playlist: hoặc 206 đúng chuẩn, hoặc KHÔNG quảng cáo accept-ranges
curl -s -o /dev/null -D - -H "Range: bytes=200-" "$VARIANT" \
 | grep -iE '^HTTP/|content-length|accept-ranges'
# KHÔNG được: 200 + full body + accept-ranges: bytes


# 6. Keep-alive
curl -s -o /dev/null -D - "$VARIANT" | grep -i 'connection'
# kỳ vọng: không có 'Connection: close'
```


---


## 8. Thứ tự ưu tiên đề nghị


| # | Việc | Mức | Hệ quả nếu bỏ |
|---|---|---|---|
| 1 | `#EXT-X-ENDLIST` + `PLAYLIST-TYPE:VOD` mọi playlist | BẮT BUỘC | Video **hỏng hẳn** sau 14s |
| 2 | Xác nhận & xử lý giới hạn connection (mục 6) | BẮT BUỘC | success rate 37% |
| 3 | Master playlist multivariant ≥3 tier | BẮT BUỘC | Mất ABR → stall khi mạng yếu |
| 4 | Feed JSON đúng cấu trúc (mục 5) | BẮT BUỘC | Feed rỗng âm thầm |
| 5 | Range trên segment trả `206` | NÊN CÓ | Preload tải cả file thay vì 1 đoạn |
| 6 | Không quảng cáo `accept-ranges` sai trên playlist | NÊN CÓ | Nguy cơ tái hiện lỗi #1 |
| 7 | Đóng gói byte-range fMP4 một file | TỐI ƯU | Preload nặng hơn cần thiết |


Mục 1 và 2 nên làm trước — chúng là hai thứ đang trực tiếp khiến 63% item không phát được.




