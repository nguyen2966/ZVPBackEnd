# API & hành vi backend — tài liệu cho client

Tài liệu này mô tả **backend đang chạy thật**, không phải bản dự kiến. Mọi ví dụ JSON bên dưới
được copy nguyên văn từ response thật của server.

Hợp đồng gốc: [SPEC.md](SPEC.md). Yêu cầu định dạng HLS: [des.md](des.md).
Cách expose server ra thiết bị thật: [../SERVING.md](../SERVING.md).

---

## 1. Bắt đầu nhanh

| | |
|---|---|
| Base URL (dev, cùng máy) | `http://localhost:3000` |
| Media | VNDATA S3 — `https://s3-hcm-r2.s3cloud.vn/zvideo-media/…` |
| Base URL (điện thoại cùng WiFi) | `http://<IP-LAN>:3000` — server in ra khi khởi động |
| Content type | `application/json; charset=utf-8` |
| Auth | `Authorization: Bearer <accessToken>` cho **mọi** endpoint trừ `/api/auth/*` |
| Tài khoản test | `khoa` / `password123`, `demo` / `password123` |

```bash
TOKEN=$(curl -s localhost:3000/api/auth/login -H 'content-type: application/json' \
  -d '{"username":"khoa","password":"password123","deviceId":"devA"}' | jq -r .accessToken)

curl -s localhost:3000/api/feed -H "authorization: Bearer $TOKEN" | jq '.items | length'   # 10
```

`GET /health` không cần auth, dùng để kiểm tra server + DB còn sống:

```json
{ "status": "ok", "readyVideos": 200, "videos": 200, "users": 21, "hlsAssets": 102 }
```

### URL của asset trỏ thẳng tới VNDATA S3

`playbackAsset.url` và `thumbnailAsset.url` là **URL tuyệt đối tới VNDATA S3**, không đi qua
backend:

```
https://s3-hcm-r2.s3cloud.vn/zvideo-media/hls/<video_id>/master.m3u8
https://s3-hcm-r2.s3cloud.vn/zvideo-media/thumbnails/<video_id>.jpg
```

Client **không cần** ghép base URL, cứ dùng thẳng URL trong response. Media không phụ thuộc vào
việc backend đang chạy ở localhost, IP LAN hay tunnel — chỉ JSON đi qua backend.

---

## 2. Quy ước chung

**Timestamp** — ISO-8601 UTC kèm mili giây và hậu tố `Z`: `2026-08-14T09:12:03.412Z`.
Không dùng epoch. Client gửi lên cũng phải đúng dạng này.

**Số** — `Int` cho counter, `Long` (ms) cho `durationMs`. Không bao giờ là string.

**Field lạ** — client bỏ qua an toàn. Backend thêm field mới sẽ không làm hỏng client.

**Shape lỗi** — mọi lỗi đều đúng một dạng:

```json
{ "error": { "code": "SESSION_REVOKED", "message": "Signed in on another device" } }
```

---

## 3. Xác thực

### Vòng đời token

```
login ──► accessToken (1 giờ) + refreshToken
             │
             ├─ 401 TOKEN_EXPIRED   ──► POST /api/auth/refresh ──► accessToken mới
             │
             └─ 401 SESSION_REVOKED ──► DỪNG retry, bắt user login lại
```

> **Chi tiết quan trọng nhất của toàn bộ API client-side.**
> Hai `code` này cùng nằm trên HTTP 401 nhưng ý nghĩa trái ngược. Gộp chúng lại thì client sẽ
> đốt hết số lần retry để refresh một session đã chết vĩnh viễn.

### Mỗi user chỉ một session active

Login thành công sẽ **revoke toàn bộ session cũ** của user đó (`revoked_reason = 'NEW_LOGIN'`).
Ràng buộc này do database ép bằng partial unique index, không code path nào lách được.

Hệ quả client cần chuẩn bị: đang dùng máy A, user login máy B → **mọi request từ máy A** (kể cả
`/api/auth/refresh`) trả `401 SESSION_REVOKED` ngay lập tức. Đây là hành vi đúng, không phải lỗi.

### `POST /api/auth/register`

Không cần auth.

```json
// request
{ "username": "khoa", "password": "…", "displayName": "Khoa Nguyen" }
// 201
{ "userId": "3f1a…", "username": "khoa", "displayName": "Khoa Nguyen" }
```

`409 USERNAME_TAKEN` nếu username đã tồn tại.

### `POST /api/auth/login`

Không cần auth.

```json
// request
{ "username": "khoa", "password": "password123", "deviceId": "devA" }
```
```json
// 200
{
  "userId": "49a1f4c2-21e2-49c7-8e28-2617592b1b04",
  "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6...",
  "refreshToken": "dFQH0XDqDJFNCJHW...",
  "expiresInSeconds": 3600
}
```

`401` nếu sai username hoặc password.

### `POST /api/auth/refresh`

```json
// request
{ "refreshToken": "…" }
// 200
{ "accessToken": "…", "expiresInSeconds": 3600 }
```

`401 SESSION_REVOKED` nếu refresh token thuộc session đã bị revoke.

---

## 4. `GET /api/feed`

Cần auth. **Không tham số.** Trả đúng 10 item random trong pool 200 video `status = READY`.

Response chỉ có duy nhất key `items` — **không có** `cursor`, `hasMore`, `page`, `offset`,
`serverScore`. Feed là random có chủ đích (xem mục 8).

```json
{
  "items": [
    {
      "position": 0,
      "video": {
        "id": "VeoACWts2F4_2",
        "user": {
          "id": "38a2cecc-26d2-4702-81ba-dbf139af6366",
          "displayName": "Revive Music",
          "username": "revivemusic",
          "avatarUrl": "https://res.cloudinary.com/…/cld-sample.jpg"
        },
        "category": { "name": "Food" },
        "title": "CƠM 20K Ở SÀI GÒN THÌ NHƯ THẾ NÀO ?? || SHORTS",
        "caption": "CƠM 20K Ở SÀI GÒN THÌ NHƯ THẾ NÀO ?? || SHORTS",
        "durationMs": 59000,
        "playbackAsset":  { "url": "http://127.0.0.1:3000/video/upload/sp_auto/VeoACWts2F4.m3u8" },
        "thumbnailAsset": { "url": "http://127.0.0.1:3000/video/upload/so_auto/VeoACWts2F4.jpg" },
        "engagement": { "likeCount": 0, "dislikeCount": 0, "bookmarkCount": 0 },
        "viewerState": { "isBookmarked": false, "reaction": null }
      }
    }
  ]
}
```

| Field | Ghi chú |
|---|---|
| `position` | 0-based **trong array trả về**, không phải thứ hạng toàn cục |
| `video.id` | Luôn có, không rỗng. Là khoá dùng cho mọi mutation reaction |
| `playbackAsset.url` | Luôn có. HLS **master playlist multivariant** (mục 7) |
| `category.name` | Luôn có |
| `caption` | Có thể là chuỗi rỗng `""`, không bao giờ `null` |
| `viewerState.reaction` | `"LIKE"` \| `"DISLIKE"` \| `null` — trạng thái của **chính user đang gọi** |
| `viewerState.isBookmarked` | `true` \| `false` |
| `engagement.*` | Số nguyên, đã clamp ≥ 0 |

`position` được đánh lại từ 0 cho mỗi lần gọi, nên không dùng nó làm khoá dedup — dùng `video.id`.

**Feed có thể trả trùng video giữa các lần gọi.** Không có state phân trang, không lọc video đã
xem. Client tự dedup và tự lọc.

---

## 4b. `GET /api/users/{userId}/bookmarks`

Cần auth. Trả **toàn bộ** video user đó đã bookmark, **đúng cùng shape với `/api/feed`** —
dùng chung parser, chung màn hình player, không cần code riêng.

```json
{
  "items": [
    { "position": 0, "video": { /* y hệt object video ở mục 4 */ } }
  ]
}
```

| | |
|---|---|
| Thứ tự | Mới bookmark nhất lên đầu (theo `clientUpdatedAt`), **không** random như `/api/feed` |
| Phân trang | Không có. Trả hết, giống `GET /api/reactions` |
| `viewerState.isBookmarked` | Luôn `true` với mọi item ở đây |
| `viewerState.reaction` | Vẫn là `LIKE`/`DISLIKE`/`null` thật của user |
| Chưa bookmark gì | `200` với `items: []`, **không** phải `404` |

**Chỉ xem được bookmark của chính mình.** `userId` phải khớp user trong access token, không
thì trả `403 FORBIDDEN` — kể cả khi `userId` không tồn tại (không tiết lộ user nào có thật).
Bookmark là dữ liệu riêng tư; nếu sau này cần mở cho hồ sơ công khai thì phải bỏ ràng buộc
này một cách có chủ đích ở phía server.

| Trường hợp | Kết quả |
|---|---|
| Thiếu/hết token | `401` |
| `userId` của người khác | `403 FORBIDDEN` |
| `userId` không đúng dạng UUID | `400 INVALID_REQUEST` |

Bỏ bookmark (`type=BOOKMARK, active=false`) thì item biến mất khỏi endpoint này ngay — server
vẫn giữ tombstone nội bộ cho LWW nhưng không lộ ra (bất biến 4).

Video đã xoá (`status = DELETED`) bị loại khỏi danh sách. Video đang `PROCESSING` thì **vẫn
hiện**, để bookmark của user không im lặng biến mất trong lúc chờ xử lý.

---

## 4c. `GET /api/categories`

Cần auth. **Không tham số.** Trả toàn bộ category hiện có để client lưu và sử dụng cho ranking,
filter hoặc màn hình chọn category mà không phải hard-code danh sách trong app.

```json
{
  "items": [
    { "id": 1, "name": "Food" },
    { "id": 2, "name": "Fun and Memes" }
  ]
}
```

Response chỉ có duy nhất key `items`. Mỗi item chỉ có đúng hai field:

| Field | Kiểu | Ghi chú |
|---|---|---|
| `id` | `Int` | ID của category trong database; client dùng làm định danh |
| `name` | `String` | Tên category; dữ liệu hiện tại không rỗng, database đảm bảo unique |

- Danh sách được sắp xếp tăng dần theo `name`, sau đó theo `id` nếu tên bằng nhau.
- Nếu database chưa có category nào, endpoint trả `200` với `{ "items": [] }`, không trả `404`.
- Thiếu, hết hạn hoặc sai access token thì trả `401` theo quy ước chung.

Ví dụ:

```bash
curl -s localhost:3000/api/categories \
  -H "authorization: Bearer $TOKEN" | jq
```

---

## 4d. Resumable video upload

Cần auth. Đây là upload path chính cho client mới. Một video tương ứng với **một upload session**;
các part được lưu thành file tạm trên backend, không tạo row database riêng cho từng part.

```text
POST initialize -> PUT từng part -> GET inspect khi cần resume -> POST complete
       UPLOADING ---------------------------------------------> PROCESSING
                                                                  |
                                        convert HLS + upload S3 --+-> READY | FAILED
```

Client tự sinh một UUID ổn định làm `uploadId` và giữ nguyên UUID đó khi retry. `partSize` phải lấy
từ response khởi tạo, không hard-code ở client. MVP chỉ đảm bảo resume trong cùng phiên backend;
restart/redeploy có thể làm mất part tạm vì Render dùng ephemeral disk.

### 4d.1 `POST /api/video-uploads`

Khởi tạo upload bằng `multipart/form-data`. Request này chỉ gửi metadata và thumbnail, **không gửi
MP4**.

| Field | Kiểu | Ghi chú |
|---|---|---|
| `uploadId` | UUID | Client sinh một lần và giữ nguyên qua retry |
| `title` | String | Bắt buộc, không được rỗng sau khi trim |
| `caption` | String | Tuỳ chọn, mặc định `""` |
| `categoryId` | Int | Phải tồn tại trong database |
| `fileSize` | Int64 | Dung lượng MP4 theo byte, tối đa theo `MAX_UPLOAD_MB` |
| `thumbnail` | JPEG | Tối đa 2 MiB |

Backend tạo video ở trạng thái `UPLOADING`, upload thumbnail lên VNData và tạo workspace tạm.

```json
// 201 Created
{
  "uploadId": "1d41ea51-9a6a-48c1-8b8d-1b3c318cb491",
  "videoId": "up_a1b2c3d4e5f",
  "status": "UPLOADING",
  "partSize": 8388608
}
```

Gửi lại đúng `uploadId` và cùng metadata là idempotent: backend trả `200` với cùng `videoId`.
Nếu UUID đã được dùng cho nội dung khác, backend trả `409 UPLOAD_CONFLICT`.

### 4d.2 `PUT /api/video-uploads/{uploadId}/parts/{partNumber}`

Body là raw bytes với `Content-Type: application/octet-stream`. `partNumber` bắt đầu từ `1`.
Mọi part trừ part cuối phải đúng bằng `partSize`; part cuối bằng số byte còn lại.

```text
204 No Content
```

Backend ghi part theo cách atomic. Gửi lại cùng một part là an toàn và thay thế bản cũ; client chỉ
tăng progress sau khi nhận `204`.

### 4d.3 `GET /api/video-uploads/{uploadId}`

Dùng sau khi request lỗi hoặc mất response để biết part nào backend đã nhận:

```json
{
  "uploadId": "1d41ea51-9a6a-48c1-8b8d-1b3c318cb491",
  "videoId": "up_a1b2c3d4e5f",
  "status": "UPLOADING",
  "partSize": 8388608,
  "uploadedParts": [1, 2, 3],
  "missingParts": [4, 5]
}
```

Client chỉ gửi lại `missingParts`. Khi video không còn ở `UPLOADING`, hai mảng part được trả rỗng
vì workspace không còn là nguồn trạng thái cần thiết.

### 4d.4 `POST /api/video-uploads/{uploadId}/complete`

Backend kiểm tra đủ part, ghép thành `original.mp4`, chuyển video sang `PROCESSING` và trả:

```json
// 202 Accepted
{
  "uploadId": "1d41ea51-9a6a-48c1-8b8d-1b3c318cb491",
  "videoId": "up_a1b2c3d4e5f",
  "status": "PROCESSING",
  "partSize": 8388608
}
```

Sau khi HTTP response kết thúc, backend lần lượt:

1. kiểm tra video bằng `ffprobe`;
2. convert MP4 thành HLS multivariant;
3. upload và verify HLS trên VNData;
4. cập nhật `durationMs` và status thành `READY`, hoặc `FAILED` nếu xử lý lỗi;
5. xoá workspace upload và HLS tạm trên backend.

Gọi lại `complete` không enqueue công việc trùng: `PROCESSING` trả `202`; `READY`/`FAILED` trả
`200` với trạng thái hiện tại. Thiếu part trả `409 UPLOAD_INCOMPLETE`.

Client không cần poll liên tục. Màn hình "Video của tôi" dùng `GET /api/users/{userId}/videos`
để đọc trạng thái mới khi màn hình xuất hiện hoặc khi user chủ động refresh.

Endpoint cũ `POST /api/videos` vẫn được giữ tạm thời để rollback trong lúc xác nhận luồng iOS mới,
nhưng không nên dùng cho implementation mới.

---

## 5. Reaction

Ba loại: `LIKE`, `DISLIKE`, `BOOKMARK`. Mỗi `(user, video, type)` là một trạng thái bật/tắt độc lập,
trừ cặp LIKE/DISLIKE loại trừ nhau (mục 5.4).

### 5.1 `POST /api/reactions`

Cần auth. Nhận **một array** để flush cả queue offline trong một round trip.

```json
{
  "mutations": [
    { "mutationId": "9f1c…", "videoId": "VeoACWts2F4_2", "type": "LIKE",
      "active": true, "clientUpdatedAt": "2026-08-13T13:10:29.424Z" }
  ]
}
```

| Field | Ý nghĩa |
|---|---|
| `mutationId` | UUID do client sinh **mới cho mỗi hành động**. Chỉ để trace, server không dùng làm khoá chống trùng |
| `videoId` | `video.id` lấy từ feed |
| `type` | `LIKE` \| `DISLIKE` \| `BOOKMARK` |
| `active` | `true` = bật reaction, `false` = bỏ reaction |
| `clientUpdatedAt` | **Thời điểm user bấm**, không phải lúc gửi request. Đây là khoá quyết định LWW |

- Tối đa **200 mutation** mỗi request, vượt thì `400 BATCH_TOO_LARGE`.
- Toàn bộ batch áp trong **một transaction**, theo đúng thứ tự trong array.
- **Luôn trả `200`** khi request hợp lệ; lỗi báo theo từng item.

> `clientUpdatedAt` phải là lúc user bấm. Nếu client điền thời điểm gửi request, mọi hành động
> offline sẽ luôn "thắng" oan khi đồng bộ muộn, và cơ chế LWW mất tác dụng.

Response — trạng thái từng item + counter mới nhất của mọi video bị chạm tới:

```json
{
  "results": [
    { "mutationId": "e456d179-…", "status": "APPLIED" },
    { "mutationId": "f185e07e-…", "status": "REJECTED", "reason": "VIDEO_NOT_FOUND" }
  ],
  "videos": [
    { "id": "VeoACWts2F4_2", "likeCount": 1, "dislikeCount": 0, "bookmarkCount": 0 }
  ]
}
```

| `status` | Nghĩa | Client làm gì |
|---|---|---|
| `APPLIED` | Đã ghi | Đánh dấu đã sync |
| `STALE` | Server đang giữ giá trị mới hơn hoặc bằng | Lấy `current` làm sự thật, ghi đè state local |
| `REJECTED` | Mutation không hợp lệ | **Bỏ hẳn, không retry** |

`STALE` luôn kèm `current` là trạng thái server đang giữ:

```json
{
  "mutationId": "90fefd69-…",
  "status": "STALE",
  "current": { "active": true, "clientUpdatedAt": "2026-08-13T13:10:29.424Z" }
}
```

Lý do `REJECTED`: `VIDEO_NOT_FOUND` (video không tồn tại hoặc đã xoá) · `INVALID_TYPE`
(type ngoài 3 giá trị hợp lệ) · `INVALID_TIMESTAMP` (không parse được, hoặc lệch quá 1 năm so với
giờ server).

**`videos` là nguồn sự thật cho số đếm.** Đây là counter *sau khi commit*. Client dùng nó thay cho
con số optimistic đang hiển thị. Bỏ qua nó thì số đếm sẽ lệch 1 cho tới lần fetch feed sau.

### 5.2 Idempotency — gửi lại cùng một mutation là an toàn

Retry sau timeout **không bao giờ đếm đôi**. Gửi lại y nguyên mutation cũ (cùng
`clientUpdatedAt`) thì server trả `STALE` và counter đứng yên.

Thực đo trên server đang chạy:

| # | Gửi | Kết quả | `likeCount` |
|---|---|---|---|
| 1 | `LIKE active=true` @ `T` | `APPLIED` | 0 → **1** |
| 2 | y nguyên request #1 | `STALE` | **1** (không nhích) |
| 3 | `LIKE active=false` @ `T-1h` | `STALE` | **1** (lệnh cũ bị bỏ qua) |
| 4 | `LIKE active=false` @ `T+1h` | `APPLIED` | 1 → **0** |

Vì vậy client cứ retry thoải mái khi mất mạng — miễn giữ nguyên `clientUpdatedAt` của lần user
bấm đầu tiên, **không** cập nhật lại timestamp mỗi lần retry.

### 5.3 Last-write-wins

Xung đột giải theo `clientUpdatedAt`, **không** theo thứ tự request tới server. Mutation có
timestamp cũ hơn hoặc bằng giá trị server đang giữ sẽ bị bỏ qua và trả `STALE` (hàng #3 ở bảng trên).

Đây là thứ giữ cho dữ liệu đúng khi user thao tác offline trên hai thiết bị rồi đồng bộ lệch giờ.

Đồng hồ client chạy nhanh sẽ bị kẹp về `giờ server + 2 phút`. Gửi timestamp tương lai xa không
giúp "thắng" LWW, chỉ làm mọi mutation sau đó trong cùng khoảng bị kẹp bằng nhau.

### 5.4 LIKE và DISLIKE loại trừ nhau

Set `LIKE active=true` thì server **tự tắt** `DISLIKE` của cùng video trong cùng transaction, và
ngược lại. Client chỉ cần gửi một mutation.

Nếu client vẫn gửi tường minh mutation tắt cái kia (với cùng `clientUpdatedAt`) thì cũng không sao —
lần áp thứ hai là no-op. Hai bên không đánh nhau.

Không bao giờ có chuyện `GET /api/reactions` trả về cả LIKE lẫn DISLIKE cho cùng một video.

### 5.5 `GET /api/reactions`

Cần auth. Không tham số, không phân trang. Trả **toàn bộ** reaction đang bật của user.

Dùng khi vừa login trên thiết bị mới (cache local trống): response đã nhúng sẵn `video` nên client
vẽ được ngay màn hình bookmark mà không phải bắn thêm N request.

```json
{
  "items": [
    {
      "videoId": "VeoACWts2F4",
      "type": "BOOKMARK",
      "clientUpdatedAt": "2026-08-14T17:07:35.532Z",
      "video": {
        "id": "VeoACWts2F4",
        "title": "CƠM 20K Ở SÀI GÒN THÌ NHƯ THẾ NÀO ?? || SHORTS",
        "thumbnailUrl": "http://127.0.0.1:3000/video/upload/so_auto/VeoACWts2F4.jpg",
        "durationMs": 59000,
        "category": "Food",
        "creator": {
          "id": "8857d3fb-…",
          "displayName": "THƯ VIỆN PHÁP LUẬT",
          "username": "thvinphplut",
          "avatarUrl": "https://res.cloudinary.com/…/messi_afp8ax.webp"
        },
        "engagement": { "likeCount": 0, "dislikeCount": 1, "bookmarkCount": 1 }
      }
    }
  ]
}
```

Lưu ý shape ở đây **khác** feed, đừng dùng chung parser:

| Feed | GET /api/reactions |
|---|---|
| `video.user` | `video.creator` |
| `video.thumbnailAsset.url` | `video.thumbnailUrl` |
| `video.category.name` | `video.category` (string) |
| có `playbackAsset` | **không có** `playbackAsset` |

Chỉ trả row đang bật. Bỏ reaction rồi thì entry biến mất khỏi đây, kể cả server vẫn giữ row nội bộ
để phục vụ LWW. Video đã xoá cũng bị loại khỏi response.

---

## 6. `GET /api/config`

Cần auth. Trả bundle config đang bật, kèm `ETag`.

```json
{
  "version": 1,
  "ttlSeconds": 900,
  "payload": {
    "feed":    { "fallbackTimeoutMs": 1200, "pageSize": 10, "maxWindow": 100 },
    "ranking": {
      "positiveCompletionRate": 0.6,
      "minPlaybackMsForSession": 0,
      "enabled": ["likedChannel", "dislikedChannel", "positiveChannel",
                  "likedCategory", "dislikedCategory", "positiveCategory"],
      "weights": { "likedChannel": 1.0, "dislikedChannel": -1.5, "positiveChannel": 0.8,
                   "likedCategory": 0.6, "dislikedCategory": -0.8, "positiveCategory": 0.5 }
    },
    "sync":  { "batchSize": 50, "debounceMs": 400, "maxAttempts": 8 },
    "cache": { "videoTtlHours": 72, "maxCachedVideos": 200,
               "sessionTtlDays": 90, "maxSessions": 5000 }
  }
}
```

- `ETag` hiện tại: `W/"config-v1"`, derive từ `version`. Gửi lại qua `If-None-Match` → `304`.
- **Không bao giờ trả `404`.** Chưa có bundle nào bật thì trả `payload: {}` với `version = 0`.
  Client dùng default compile-in cho các key bị thiếu.
- `payload` là opaque với server — đổi trọng số ranking không cần release app, chỉ cần tăng
  `version` và bật bundle mới.
- `ranking.*` là tham số cho ranking engine **chạy hoàn toàn ở client**.

---

## 7. Asset HLS

`playbackAsset.url` trỏ tới **master playlist multivariant**, đúng định dạng mô tả trong
[des.md](des.md).

```
<S3>/hls/<id>/master.m3u8              master, 3–5 rendition
<S3>/hls/<id>/<tier>/index.m3u8        media playlist từng rendition
<S3>/hls/<id>/<tier>/<id>.mp4dv        1 file fMP4/rendition, cắt bằng byte-range
<S3>/thumbnails/<id>.jpg               thumbnail 
```

URI variant trong master là **tương đối** (`pg_5/index.m3u8`), resolve theo baseUri của master —
Media3 xử lý sẵn bằng `UriUtil.resolve`, nhưng script test tự viết thì phải nhớ điều này.

Client dựa được vào các bảo đảm sau (đã kiểm trên toàn bộ 200 video):

- Master luôn ≥ 3 rendition, mỗi `#EXT-X-STREAM-INF` đều có `BANDWIDTH` (và `RESOLUTION`, `CODECS`).
  Ladder tối đa 5 tier `pg_1`…`pg_5` (180×320 → 720×1280); video nguồn nhỏ hơn thì ít tier hơn,
  không bao giờ dưới 3. Thực tế trên pool 200 video: 188 video 5 tier, 11 video 4 tier, 1 video 3 tier.
- Mọi media playlist có `#EXT-X-PLAYLIST-TYPE:VOD` **và** `#EXT-X-ENDLIST` → Media3 không bao giờ
  hiểu nhầm là LIVE và ném `PlaylistStuckException`.
- `#EXT-X-TARGETDURATION:4`, segment 4s, đóng gói `#EXT-X-MAP` + `#EXT-X-BYTERANGE` trong một file
  `.mp4dv` cho mỗi rendition.
- Segment trả `206 Partial Content` + `Content-Range` cho request có header `Range` → preload theo
  byte-range hoạt động đúng.
- Playlist cũng hỗ trợ Range đúng chuẩn (trả `206` thật, không phải "200 + full body").
- Không có `Connection: close`; keep-alive bật.
- CORS expose sẵn `ETag`, `Content-Range`, `Accept-Ranges`, `Retry-After`.

Ladder **ổn định theo thời gian** cho cùng một video, nên cache playlist trong RAM là an toàn.

---

## 8. Backend cố tình KHÔNG làm

Không phải thiếu, mà vì chúng thuộc về client — làm ở server sẽ xung đột.

| Không có | Vì sao |
|---|---|
| Ranking, personalization, `serverScore` | Ranking chạy hoàn toàn ở client, dựa trên dữ liệu xem lưu local mà server không có |
| Lọc video đã xem | Server không có khái niệm "đã xem" |
| `cursor` / `hasMore` / `page` / `offset` | Feed là random, cố tình không có state phân trang |
| Phân trang cho `/api/reactions` | Toàn bộ tập chỉ cỡ vài trăm row |
| Trả tombstone ra API | Row đã tắt là chuyện nội bộ của server |
| `4xx` cho một item reaction lỗi | Sẽ chặn queue offline của client vĩnh viễn |
| Ghi log/analytics lượt xem | Chưa có trong phạm vi |

---

## 9. Bảng lỗi

| HTTP | `code` | Khi nào | Client làm gì |
|---|---|---|---|
| `400` | `INVALID_REQUEST` | Body sai shape | Sửa payload, không retry |
| `400` | `BATCH_TOO_LARGE` | > 200 mutation | Chia nhỏ batch |
| `400` | `INVALID_PART_SIZE` | Part không đúng số byte backend yêu cầu | Tạo lại đúng byte range rồi gửi lại part đó |
| `401` | `TOKEN_EXPIRED` | Token hết hạn / thiếu / hỏng | Refresh rồi thử lại |
| `401` | `SESSION_REVOKED` | Đã login ở thiết bị khác | **Dừng retry**, bắt user login lại |
| `403` | `FORBIDDEN` | | |
| `404` | `NOT_FOUND` | Asset không tồn tại. Không dùng cho `/api/config` | |
| `409` | `USERNAME_TAKEN` | Register trùng username | |
| `409` | `UPLOAD_CONFLICT` | `uploadId` đã được dùng cho metadata khác | Không tạo UUID mới khi chỉ retry cùng upload |
| `409` | `UPLOAD_NOT_ACTIVE` | Video không còn ở `UPLOADING` | Inspect để lấy trạng thái hiện tại |
| `409` | `UPLOAD_EXPIRED` | Upload session đã hết hạn | Khởi tạo upload mới |
| `409` | `UPLOAD_INCOMPLETE` | Complete khi còn thiếu/sai part | Inspect rồi gửi lại `missingParts` |
| `429` | `RATE_LIMITED` | Kèm header `Retry-After` | Đọc `Retry-After` để tính lúc thử lại |
| `500` | `UPLOAD_INITIALIZATION_FAILED` | Không tạo được workspace hoặc upload thumbnail | Retry initialization với cùng `uploadId` |
| `500` | `INTERNAL` | | |

Hiện **chưa bật rate limit**. Nếu bật sau này thì `429` sẽ luôn kèm `Retry-After`.

---



## 10. Sai khác so với SPEC.md

Ba điểm backend làm khác tài liệu gốc, đều có chủ đích:

| Sai khác | Lý do |
|---|---|
| **9 category** thay vì 5–8 | Lấy đúng category có thật trong dữ liệu |
| `durationMs` nằm trong khoảng **8.000–180.000** thay vì 15.000–60.000 | Lấy đúng độ dài thật của media. `durationMs` sai sẽ làm hỏng completion rate của ranking, nên độ chính xác được ưu tiên hơn việc khớp khoảng đề xuất |

Ngoài ra: creator được gom về 15 người (dữ liệu gốc có 90 uploader) để mỗi creator có 13–14 video —
nếu không, tiêu chí "channel xem nhiều nhất" của client sẽ không bao giờ kích hoạt.

---

## 11. Checklist tích hợp

- [ ] Lưu cả `accessToken` và `refreshToken` sau login
- [ ] Phân biệt `TOKEN_EXPIRED` (refresh) và `SESSION_REVOKED` (dừng hẳn, login lại)
- [ ] Dedup feed theo `video.id`, **không** theo `position`
- [ ] Fetch `GET /api/categories` và lưu theo `id`; không hard-code danh sách category trong app
- [ ] Dùng thẳng `playbackAsset.url`, không tự ghép base URL
- [ ] `clientUpdatedAt` = lúc **user bấm**, giữ nguyên qua mọi lần retry
- [ ] Lấy số đếm từ mảng `videos` trong response, không tự cộng trừ optimistic mãi
- [ ] Xử lý `STALE` bằng cách nhận `current` làm sự thật
- [ ] `REJECTED` thì bỏ hẳn khỏi queue, không retry
- [ ] Batch ≤ 200 mutation
- [ ] Upload mới: khởi tạo `POST /api/video-uploads`, gửi các part còn thiếu, rồi gọi `/complete`
- [ ] Lấy `partSize` từ response; giữ nguyên `uploadId` và file local để retry/resume trong phiên
- [ ] Sau `202 PROCESSING`, đọc trạng thái qua màn hình "Video của tôi"; không poll liên tục
- [ ] Màn hình "Video của tôi": `GET /api/users/{userId}/videos` (có `status` để hiện video đang xử lý)
- [ ] Màn hình bookmark: dùng `GET /api/users/{userId}/bookmarks` (cùng shape feed, không cần parser riêng)
- [ ] Parser riêng cho `GET /api/reactions` (`creator`/`thumbnailUrl`/`category` khác feed)
- [ ] Cho phép cleartext HTTP nếu test qua LAN (xem [../SERVING.md](../SERVING.md))
