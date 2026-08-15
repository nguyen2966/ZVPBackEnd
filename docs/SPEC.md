## 0. Phạm vi

Backend cho một app video ngắn: pool video cố định, feed random, đồng bộ reaction 2 chiều, và remote
config cho client.


**Server chịu trách nhiệm:** lưu trữ, xác thực, LWW resolve xung đột reaction, đếm engagement, phục vụ
config.


**Server KHÔNG làm** (mục 9 nói rõ hơn): ranking, cá nhân hoá, lọc video đã xem, phân trang.


### Giả định kỹ thuật


| Hạng mục | Chốt |
|---|---|
| Database | PostgreSQL 14+ (dùng `jsonb`, partial index, `on conflict ... where`) |
| Framework HTTP | tự chọn |
| Content type | `application/json; charset=utf-8` |
| Timestamp | ISO-8601 kèm offset, luôn UTC: `2026-08-14T09:12:03.412Z`. Không dùng epoch |
| Auth | `Authorization: Bearer <accessToken>` cho **mọi** endpoint trừ `/api/auth/*` |
| Số | `Int` cho counter, `Long` (ms) cho duration |


**Bất biến quan trọng nhất:** client đã được viết theo đúng shape JSON ở mục 3. Sai tên field là
client parse ra rỗng **mà không báo lỗi**. Bám sát từng chữ.


---


## 1. Tám bất biến không được phá


1. **Feed trả đúng N item random** trong pool `status = 'READY'`. Không cursor, không `hasMore`,
  không lọc trùng giữa các lần gọi. Client tự dedup.
2. **Mỗi `(user, video, type)` là một row reaction duy nhất.** `type ∈ {LIKE, DISLIKE, BOOKMARK}`.
3. **Row reaction không bao giờ bị `DELETE` khi user bỏ reaction** — set `active = false`. Cần
  timestamp của lần bỏ để LWW từ chối được lệnh cũ tới muộn (bất biến 5).
4. **`GET /api/reactions` chỉ trả row `active = true`.** Tombstone là chuyện nội bộ của server.
5. **Xung đột giải bằng last-write-wins theo `clientUpdatedAt`** — thời điểm **user bấm**, không phải
  thời điểm request tới. Một mutation có `clientUpdatedAt` cũ hơn giá trị đang lưu bị bỏ qua và trả
  về `STALE`.
6. **Set LIKE thì tắt DISLIKE và ngược lại**, trong cùng transaction (mục 4.3).
7. **`POST /api/reactions` luôn trả `200` kèm status theo từng item** khi request hợp lệ. Một item lỗi
  không được làm cả batch lỗi — client dùng batch để flush queue offline, và một item bị từ chối
  vĩnh viễn sẽ chặn queue đó mãi mãi.
8. **Mỗi user chỉ một session active.** Login mới revoke mọi session cũ; request từ session đã revoke
  trả `401` với `code = "SESSION_REVOKED"`.


---


## 2. Schema


DDL chạy được nguyên khối.


### 2.1 Users, sessions


```sql
create extension if not exists pgcrypto;   -- gen_random_uuid()


create table users (
 id            uuid primary key default gen_random_uuid(),
 username      text not null unique,
 display_name  text not null,
 avatar_url    text,
 password_hash text not null,             -- bcrypt/argon2. Không bao giờ lưu plaintext
 created_at    timestamptz not null default now()
);


create table sessions (
 id                 uuid primary key default gen_random_uuid(),
 user_id            uuid not null references users(id) on delete cascade,
 device_id          text not null,
 refresh_token_hash text not null,        -- hash, không lưu token gốc
 created_at         timestamptz not null default now(),
 last_seen_at       timestamptz not null default now(),
 revoked_at         timestamptz,
 revoked_reason     text                  -- 'NEW_LOGIN' | 'LOGOUT' | 'ADMIN'
);


-- Bất biến 8, do database ép: không code path nào tạo được session active thứ hai.
create unique index sessions_one_active_per_user
 on sessions (user_id) where revoked_at is null;
```


### 2.2 Nội dung


```sql
create table categories (
 id   serial primary key,
 name text not null unique
);


create table videos (
 id            text primary key,          -- id ổn định, là khoá tham chiếu của reaction
 creator_id    uuid not null references users(id) on delete cascade,
 category_id   int  references categories(id),
 title         text not null,
 caption       text,
 duration_ms   bigint not null check (duration_ms >= 0),
 playback_url  text not null,             -- HLS master playlist, xem BACKEND_HLS_REQUIREMENTS.md
 thumbnail_url text,
 status        text not null default 'READY'
               check (status in ('UPLOADING','PROCESSING','READY','FAILED','DELETED')),


 -- Counter denormalize, do trigger ở 4.4 cập nhật. Đặt trên `videos` thay vì tách bảng riêng:
 -- query feed cần các số này trên mọi row nên bớt được một join.
 like_count     int not null default 0,
 dislike_count  int not null default 0,
 bookmark_count int not null default 0,


 created_at    timestamptz not null default now()
);
-- Cố ý không đánh index cho query feed: `order by random()` phải quét và sort toàn bộ tập
-- `status = 'READY'`, nên không index nào giúp được. Ở pool 200 row thì việc đó là miễn phí.
```


### 2.3 Reactions


```sql
create type reaction_type as enum ('LIKE', 'DISLIKE', 'BOOKMARK');


create table reactions (
 user_id            uuid not null references users(id) on delete cascade,
 video_id           text not null references videos(id) on delete cascade,
 type               reaction_type not null,


 -- false = đã bỏ reaction. Giữ row lại (bất biến 3), không lộ ra API (bất biến 4).
 active             boolean not null,


 -- Thời điểm user bấm, do client gửi. Khoá LWW (bất biến 5).
 client_updated_at  timestamptz not null,
 client_mutation_id uuid not null,        -- để trace/log, không unique
 server_updated_at  timestamptz not null default now(),


 primary key (user_id, video_id, type)    -- bất biến 2
);


create index reactions_by_user on reactions (user_id) where active;         -- GET /api/reactions
create index reactions_counts  on reactions (video_id, type) where active;  -- rebuild counter
```


### 2.4 Config


```sql
create table app_config (
 id         serial primary key,
 version    bigint not null,
 payload    jsonb  not null,              -- server coi là opaque; shape ở mục 5
 enabled    boolean not null default false,
 updated_at timestamptz not null default now()
);


-- Đúng một bundle live tại một thời điểm.
create unique index app_config_one_live on app_config (enabled) where enabled;
```


---


## 3. API


Sai một tên field là client hỏng im lặng. Các ví dụ dưới đây là hợp đồng.


### 3.1 `POST /api/auth/register`


Không cần auth. Có thể bỏ nếu seed sẵn user (mục 6).


```json
// request
{ "username": "khoa", "password": "…", "displayName": "Khoa Nguyen" }
// 201
{ "userId": "3f1a…", "username": "khoa", "displayName": "Khoa Nguyen" }
```


`409` nếu username đã tồn tại.


### 3.2 `POST /api/auth/login`


Không cần auth.


```json
// request
{ "username": "khoa", "password": "…", "deviceId": "a1b2c3" }
// 200
{ "userId": "3f1a…", "accessToken": "…", "refreshToken": "…", "expiresInSeconds": 3600 }
```


Trong **một transaction**: xác thực → `update sessions set revoked_at = now(), revoked_reason =
'NEW_LOGIN' where user_id = $1 and revoked_at is null` → insert session mới. Thứ tự này bắt buộc, nếu
không partial unique index sẽ chặn.


`401` nếu sai thông tin.


### 3.3 `POST /api/auth/refresh`


```json
{ "refreshToken": "…" }              // → 200 { "accessToken": "…", "expiresInSeconds": 3600 }
```


`401` nếu refresh token thuộc session đã revoke — kèm `code = "SESSION_REVOKED"`.


### 3.4 `GET /api/feed`


Cần auth. Không tham số.


Trả **10 item random** trong pool `status = 'READY'`:


```sql
select v.*, c.name as category_name,
      u.id as creator_id, u.display_name, u.username, u.avatar_url
from videos v
join users u on u.id = v.creator_id
left join categories c on c.id = v.category_id
where v.status = 'READY'
order by random()
limit 10;
```


Viewer state lấy từ `reactions` của người gọi (một query nữa theo danh sách 10 id vừa chọn, `where
active`).


Response — **shape này client đã parse sẵn, giữ nguyên từng tên field**:


```json
{
 "items": [
   {
     "position": 0,
     "video": {
       "id": "v_00042",
       "user": {
         "id": "3f1a…",
         "displayName": "Khoa Nguyen",
         "username": "khoa",
         "avatarUrl": "https://…/avatar.jpg"
       },
       "category": { "name": "Music" },
       "title": "Sunset timelapse",
       "caption": "Đà Nẵng, 6:12pm",
       "durationMs": 24000,
       "playbackAsset":  { "url": "https://…/v_00042/master.m3u8" },
       "thumbnailAsset": { "url": "https://…/v_00042/thumb.jpg" },
       "engagement": { "likeCount": 41, "dislikeCount": 2, "bookmarkCount": 9 },
       "viewerState": { "isBookmarked": true, "reaction": "LIKE" }
     }
   }
 ]
}
```


Quy tắc từng field:


| Field | Bắt buộc | Ghi chú |
|---|---|---|
| `position` | có | Chỉ số 0-based **trong array trả về**, không phải thứ hạng toàn cục |
| `video.id` | **có, không được rỗng** | Rỗng → client **âm thầm loại item đó** |
| `video.playbackAsset.url` | **có, không được rỗng** | Rỗng/null → client **âm thầm loại item đó** |
| `video.category.name` | nên có | Rỗng → client thay bằng `"Uncategorized"` |
| `video.user.displayName` / `username` | nên có | Thiếu một cái, client lấy cái còn lại |
| `engagement.*` | có | Số âm bị clamp về 0 |
| `viewerState.reaction` | có | `"LIKE"` \| `"DISLIKE"` \| `null`. Giá trị lạ → hiểu là không react |
| `viewerState.isBookmarked` | có | |


Hai dòng in đậm là chế độ lỗi cần biết khi vận hành: một video thiếu `playbackAsset.url` sẽ **biến mất
khỏi feed mà không có lỗi nào** ở cả hai phía. Field lạ thì client bỏ qua, thêm field mới là an toàn.


### 3.5 `POST /api/reactions`


Cần auth. Nhận một array để client flush queue offline trong một round trip.


```json
{
 "mutations": [
   { "mutationId": "9f1c…", "videoId": "v_00042", "type": "LIKE",
     "active": true,  "clientUpdatedAt": "2026-08-14T09:12:03.412Z" },
   { "mutationId": "7a4e…", "videoId": "v_00042", "type": "DISLIKE",
     "active": false, "clientUpdatedAt": "2026-08-14T09:12:03.412Z" }
 ]
}
```


- Áp **tất cả** mutation trong **một transaction**, theo đúng thứ tự trong array.
- `mutationId` là UUID để trace; **không** dùng làm khoá unique (client sinh mới mỗi hành động).
- Giới hạn mềm 200 mutation/request; vượt thì `400` `code = "BATCH_TOO_LARGE"`.


Response — luôn `200` khi request hợp lệ (bất biến 7):


```json
{
 "results": [
   { "mutationId": "9f1c…", "status": "APPLIED" },
   { "mutationId": "7a4e…", "status": "STALE",
     "current": { "active": true, "clientUpdatedAt": "2026-08-14T12:44:01.000Z" } },
   { "mutationId": "3b2d…", "status": "REJECTED", "reason": "VIDEO_NOT_FOUND" }
 ],
 "videos": [
   { "id": "v_00042", "likeCount": 41, "dislikeCount": 2, "bookmarkCount": 9 }
 ]
}
```


| `status` | Khi nào | Client làm gì |
|---|---|---|
| `APPLIED` | upsert đã ghi | đánh dấu đã sync |
| `STALE` | server đang giữ `client_updated_at` mới hơn hoặc bằng | nhận `current` làm sự thật |
| `REJECTED` | `VIDEO_NOT_FOUND` \| `INVALID_TYPE` \| `INVALID_TIMESTAMP` | bỏ hẳn, không retry |


`videos` phải chứa counter **sau khi commit** của mọi video bị chạm tới trong batch. Client dùng nó
thay thế con số optimistic đang hiển thị; thiếu nó thì số đếm sẽ lệch 1 cho tới lần fetch feed sau.


### 3.6 `GET /api/reactions`


Cần auth. Không tham số, không phân trang. Trả **toàn bộ** reaction `active = true` của người gọi.


```json
{
 "items": [
   {
     "videoId": "v_00042",
     "type": "BOOKMARK",
     "clientUpdatedAt": "2026-08-13T22:10:00Z",
     "video": {
       "id": "v_00042",
       "title": "Sunset timelapse",
       "thumbnailUrl": "https://…/thumb.jpg",
       "durationMs": 24000,
       "category": "Music",
       "creator": { "id": "3f1a…", "displayName": "Khoa Nguyen", "username": "khoa",
                    "avatarUrl": "https://…/avatar.jpg" },
       "engagement": { "likeCount": 41, "dislikeCount": 2, "bookmarkCount": 9 }
     }
   }
 ]
}
```


`video` nhúng kèm là **bắt buộc**, không phải tuỳ chọn: client dùng response này để vẽ danh sách
bookmark trên một thiết bị vừa login (cache local trống), và không client nào nên phải bắn N request
để vẽ một screen. Vẫn trả kèm `video` cả với `type = LIKE`/`DISLIKE` — cùng một shape cho mọi row.


Nếu video đã bị xoá (`status = 'DELETED'`): bỏ hẳn row đó khỏi response.


### 3.7 `GET /api/config`


Cần auth. Trả bundle đang `enabled`.


```json
{ "version": 17, "ttlSeconds": 900, "payload": { "…": "xem mục 5" } }
```


- Hỗ trợ `ETag` + `If-None-Match` → `304` khi không đổi. `ETag` nên derive từ `version`.
- Không bao giờ trả `404`: nếu chưa có bundle nào `enabled`, trả bundle default ở mục 5 với
 `version = 0`. Client cold start có timeout ngắn cho endpoint này, `404` khiến nó phải chờ hết
 timeout một cách vô nghĩa.


---


## 4. Quy tắc nghiệp vụ


### 4.1 Câu upsert reaction


Nơi **duy nhất** LWW được quyết định. Không nhân bản logic này ra chỗ khác.


```sql
insert into reactions (user_id, video_id, type, active, client_updated_at, client_mutation_id)
values ($1, $2, $3, $4,
       -- Không tin đồng hồ client chạy nhanh: kẹp về mốc server + 2 phút.
       least($5::timestamptz, now() + interval '2 minutes'), $6)
on conflict (user_id, video_id, type) do update
  set active             = excluded.active,
      client_updated_at  = excluded.client_updated_at,
      client_mutation_id = excluded.client_mutation_id,
      server_updated_at  = now()
where excluded.client_updated_at > reactions.client_updated_at
returning *;
```


- Trả về **1 row** → `APPLIED`.
- Trả về **0 row** → đã có row với timestamp mới hơn hoặc bằng → `select` row hiện tại và trả `STALE`
 kèm giá trị đó.
- Row chưa tồn tại thì insert luôn, kể cả khi `active = false` (client đang push một lần bỏ reaction
 cho thứ server chưa từng biết — vô hại, và giữ đúng timestamp cho LWW về sau).


**Idempotency có sẵn:** gửi lại đúng mutation đó thì `excluded.client_updated_at =
reactions.client_updated_at`, mệnh đề `where` không thoả, không gì thay đổi. Nhờ vậy client retry sau
timeout mà không sợ đếm đôi. Đây là lý do counter phải do trigger quản (4.4) chứ không do handler.


### 4.2 Validate mutation


Theo thứ tự, trước khi upsert:


1. `videoId` không tồn tại hoặc `status = 'DELETED'` → `REJECTED` / `VIDEO_NOT_FOUND`.
2. `type` không thuộc enum → `REJECTED` / `INVALID_TYPE`.
3. `clientUpdatedAt` parse lỗi, hoặc lệch quá 1 năm so với `now()` → `REJECTED` /
  `INVALID_TIMESTAMP`.


Item bị `REJECTED` **không** làm rollback các item khác trong batch.


### 4.3 Loại trừ LIKE / DISLIKE (bất biến 6)


Khi một mutation set `type = LIKE, active = true`, chạy thêm ngay trong cùng transaction một upsert
tắt `DISLIKE`, dùng **cùng** `client_updated_at`, và ngược lại:


```sql
insert into reactions (user_id, video_id, type, active, client_updated_at, client_mutation_id)
values ($1, $2, 'DISLIKE', false, $5, $6)
on conflict (user_id, video_id, type) do update
  set active = false, client_updated_at = excluded.client_updated_at, server_updated_at = now()
where excluded.client_updated_at > reactions.client_updated_at;
```


Client hiện tại **cũng** gửi mutation tắt row kia một cách tường minh. Hai việc này không đánh nhau:
áp lần thứ hai với cùng timestamp thì mệnh đề `where` không thoả nên thành no-op. Vẫn phải làm ở
server, để một client đơn giản chỉ gửi một mutation cũng nhận được semantic đúng.


### 4.4 Trigger counter


```sql
create or replace function reactions_sync_counters() returns trigger as $$
declare d int;
begin
 -- 0 khi retry không đổi trạng thái → counter không nhích. Đây là điểm cốt lõi.
 d := (case when NEW.active then 1 else 0 end)
    - (case when TG_OP = 'UPDATE' and OLD.active then 1 else 0 end);


 if d <> 0 then
   if NEW.type = 'LIKE' then
     update videos set like_count = greatest(0, like_count + d) where id = NEW.video_id;
   elsif NEW.type = 'DISLIKE' then
     update videos set dislike_count = greatest(0, dislike_count + d) where id = NEW.video_id;
   else
     update videos set bookmark_count = greatest(0, bookmark_count + d) where id = NEW.video_id;
   end if;
 end if;
 return NEW;
end;
$$ language plpgsql;


create trigger reactions_counters
after insert or update of active on reactions
for each row execute function reactions_sync_counters();
```


Dùng `greatest(0, …)` thay vì `check (… >= 0)`: nếu counter lệch âm vì một lý do nào đó, một `CHECK`
sẽ làm transaction rollback → API trả `500` → client retry vô hạn với cùng payload. Ưu tiên
availability, rồi audit định kỳ bằng query rebuild:


```sql
update videos v set
 like_count     = coalesce(c.likes, 0),
 dislike_count  = coalesce(c.dislikes, 0),
 bookmark_count = coalesce(c.bookmarks, 0)
from (
 select video_id,
        count(*) filter (where type = 'LIKE')     as likes,
        count(*) filter (where type = 'DISLIKE')  as dislikes,
        count(*) filter (where type = 'BOOKMARK') as bookmarks
 from reactions where active group by video_id
) c
where c.video_id = v.id;
```


### 4.5 `order by random()`


Với pool 200 row thì miễn phí. Nó sort toàn bảng, nên nếu pool lên vài nghìn thì đổi sang
`tablesample bernoulli` hoặc random-offset. Để lại comment trong code để sau này không ai phải đi tìm
nguyên nhân của một độ trễ bí ẩn.


---


## 5. Shape của config payload


Server coi `payload` là opaque `jsonb` — không validate nội dung, không parse. Nhưng bundle default
phải được seed đúng shape dưới đây, vì client đọc theo đúng các key này (thiếu key nào thì client rơi
về default compile-in của nó, không crash).


```json
{
 "feed":    { "fallbackTimeoutMs": 1200, "pageSize": 10, "maxWindow": 100 },
 "ranking": {
   "positiveCompletionRate": 0.6,
   "minPlaybackMsForSession": 0,
   "enabled": ["likedChannel", "dislikedChannel", "mostWatchedChannel",
               "likedCategory", "dislikedCategory", "mostWatchedCategory"],
   "weights": { "likedChannel":  1.0, "dislikedChannel":  -1.5, "mostWatchedChannel":  0.8,
                "likedCategory": 0.6, "dislikedCategory": -0.8, "mostWatchedCategory": 0.5 }
 },
 "sync":  { "batchSize": 50, "debounceMs": 400, "maxAttempts": 8 },
 "cache": { "videoTtlHours": 72, "maxCachedVideos": 200,
            "sessionTtlDays": 90, "maxSessions": 5000 }
}
```


`ranking.*` là tham số cho ranking engine **chạy hoàn toàn ở client** — server chỉ chứa và phát chúng.
Đây là cơ chế để đổi trọng số/A-B mà không cần release app, nên `version` phải tăng mỗi lần đổi
`payload`, và mỗi lần đổi chỉ nên bật một bundle.


Seed:


```sql
insert into app_config (version, payload, enabled) values (1, '{…json trên…}'::jsonb, true);
```


---


## 6. Yêu cầu seed data


Feed random chỉ có ý nghĩa khi pool đủ đa dạng, và **ranking phía client sẽ vô dụng nếu pool sai
cấu trúc**:


| Hạng mục | Số lượng | Vì sao |
|---|---|---|
| Video `status = 'READY'` | **200** | Kích thước pool đã chốt |
| Creator | **10–20** | Client tính "channel xem nhiều nhất". Nếu mỗi video một creator khác nhau thì tiêu chí đó không bao giờ kích hoạt — mỗi creator cần ~10–20 video |
| Category | **5–8** | Tương tự cho "category xem nhiều nhất"; phân bố lệch nhau chút cho giống thật |
| `duration_ms` | 15.000–60.000 | Video ngắn; client tính completion rate = `watchedMs / durationMs` nên số này phải đúng, `0` sẽ làm video bị loại khỏi mọi signal ranking |
| `thumbnail_url` | mọi video | Thiếu thì client vẽ placeholder |
| `playback_url` | mọi video | Phải là HLS **multivariant** master playlist — xem `BACKEND_HLS_REQUIREMENTS.md`. Thiếu là item **biến mất khỏi feed mà không có lỗi** |


Thêm 1–2 user test có sẵn password để client đăng nhập ngay.


---


## 7. Định dạng lỗi chung


Một shape duy nhất cho mọi lỗi:


```json
{ "error": { "code": "SESSION_REVOKED", "message": "Signed in on another device" } }
```


| HTTP | `code` | Khi nào |
|---|---|---|
| `400` | `INVALID_REQUEST`, `BATCH_TOO_LARGE` | body sai, batch quá lớn |
| `401` | `TOKEN_EXPIRED` | access token hết hạn → client sẽ refresh rồi thử lại |
| `401` | `SESSION_REVOKED` | session đã bị revoke bởi lần login khác. **Phải phân biệt với `TOKEN_EXPIRED`** — client dừng hẳn vòng retry và bắt user login lại, thay vì refresh vô ích |
| `403` | `FORBIDDEN` | |
| `404` | `NOT_FOUND` | không dùng cho `/api/config` (mục 3.7) |
| `409` | `USERNAME_TAKEN` | |
| `429` | `RATE_LIMITED` | **phải** kèm header `Retry-After`; client đọc nó để tính thời điểm thử lại |
| `500` | `INTERNAL` | |


Hai `code` khác nhau trên cùng `401` là chi tiết quan trọng nhất trong bảng này: gộp chúng lại thì
client sẽ đốt hết số lần retry vào một session đã chết.


---


## 8. Acceptance test


Chạy tuần tự, kiểm được toàn bộ hợp đồng.


```bash
BASE=https://your-host


# 1. Login. Lưu token.
curl -s $BASE/api/auth/login -H 'content-type: application/json' \
 -d '{"username":"khoa","password":"…","deviceId":"devA"}'
# → 200, có accessToken


# 2. Feed trả đúng 10 item, mỗi item có đủ 8 field trong `video`.
curl -s $BASE/api/feed -H "authorization: Bearer $TOKEN" | jq '.items | length'   # → 10


# 3. Hai lần gọi liên tiếp phải khác nhau (random, không cursor).
#    Trùng hoàn toàn nhiều lần là dấu hiệu thiếu `order by random()`.


# 4. Like một video.
curl -s $BASE/api/reactions -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
 -d '{"mutations":[{"mutationId":"11111111-1111-1111-1111-111111111111",
      "videoId":"v_00042","type":"LIKE","active":true,
      "clientUpdatedAt":"2026-08-14T09:00:00Z"}]}'
# → results[0].status = "APPLIED"; videos[0].likeCount tăng 1


# 5. IDEMPOTENCY: gửi lại y nguyên request bước 4.
# → status = "STALE"; likeCount KHÔNG tăng nữa. Đây là test quan trọng nhất của cả API.


# 6. LWW: gửi lại với clientUpdatedAt CŨ HƠN (08:00:00Z), active=false.
# → status = "STALE"; like vẫn còn.


# 7. LWW hướng ngược: clientUpdatedAt MỚI HƠN (10:00:00Z), active=false.
# → "APPLIED"; likeCount giảm 1; row vẫn còn trong DB với active=false (bất biến 3).


# 8. Tombstone không lộ ra API.
curl -s $BASE/api/reactions -H "authorization: Bearer $TOKEN"
# → không có entry nào cho v_00042/LIKE


# 9. Loại trừ: LIKE=true rồi DISLIKE=true (timestamp tăng dần).
# → GET /api/reactions chỉ còn DISLIKE. Không bao giờ thấy cả hai.


# 10. Batch có item lỗi.
#     mutations = [ hợp lệ, videoId "khong-ton-tai" ]
# → HTTP 200. results = [APPLIED, REJECTED/VIDEO_NOT_FOUND]. Item hợp lệ ĐÃ được ghi.


# 11. Một session: login lại với deviceId "devB", rồi gọi /api/feed bằng token của devA.
# → 401 code = "SESSION_REVOKED" (không phải TOKEN_EXPIRED)


# 12. Config.
curl -si $BASE/api/config -H "authorization: Bearer $TOKEN"    # → 200 + ETag
curl -si $BASE/api/config -H "authorization: Bearer $TOKEN" -H 'if-none-match: <etag>'  # → 304


# 13. Bookmark rồi kiểm tra video nhúng kèm.
curl -s $BASE/api/reactions -H "authorization: Bearer $TOKEN" \
 | jq '.items[0].video | {title, thumbnailUrl, durationMs, creator}'
# → không field nào null
```


Bước 5, 6, 7 là bộ ba phải đúng. Chúng chính là toàn bộ cơ chế chống đếm đôi và chống mất dữ liệu khi
client retry hoặc khi user đổi thiết bị.


---


## 9. Những gì KHÔNG implement


Không phải bỏ vì thiếu thời gian — mà vì chúng thuộc về client, và làm ở server sẽ xung đột.


| Không làm | Lý do |
|---|---|
| Ranking, personalization, `serverScore` | Ranking chạy hoàn toàn ở client, dựa trên dữ liệu xem lưu local mà server không có |
| Lọc video đã xem | Client theo dõi lượt xem cục bộ và tự lọc; server không có khái niệm "đã xem" |
| Cursor, `hasMore`, `page`, `offset` trên `/api/feed` | Feed là random, cố tình không có trạng thái phân trang |
| Phân trang cho `/api/reactions` | Toàn bộ tập chỉ cỡ vài trăm row |
| Trả tombstone ra API | Bất biến 4 |
| Trả `4xx` cho một item reaction lỗi | Bất biến 7 — sẽ chặn queue offline của client |
| Đếm counter trong handler thay vì trigger | Retry sẽ đếm đôi (mục 4.4) |
| Suy ra thời điểm reaction từ `now()` | Phá LWW (bất biến 5): hành động offline sẽ luôn thắng oan |
| Ghi log/analytics lượt xem | Chưa có trong phạm vi |


---


## 10. Quyết định để mở cho backend


1. **TTL của access token.** 1 giờ là hợp lý; 
2. **Rate limit.** Chưa cần cho phạm vi này, nhưng nếu bật thì `POST /api/reactions` phải trả
  `Retry-After` (mục 7) — client đọc chính header đó.
3. **Video upload.** Bảng `videos` đã có `status` cho pipeline upload/transcode. Endpoint upload nằm
  ngoài tài liệu này; seed sẵn 200 video là đủ để client chạy.
4. **Xoá video.** Đặt `status = 'DELETED'` chứ đừng `DELETE` row: `reactions` có FK
  `on delete cascade`, xoá thật là mất luôn bookmark của user và client sẽ không được thông báo gì.



=
