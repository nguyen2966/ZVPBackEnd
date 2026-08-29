-- schema.sql - DDL cho backend video ngắn (theo docs/SPEC.md mục 2 và 4.4).
-- Chạy được nguyên khối, idempotent (drop rồi tạo lại) - dùng cho dev/demo.

create extension if not exists pgcrypto;   -- gen_random_uuid()

-- Xoá theo thứ tự phụ thuộc để chạy lại được nhiều lần.
drop trigger if exists reactions_counters on reactions;
drop function if exists reactions_sync_counters();
drop trigger if exists app_config_entries_bump on app_config_entries;
drop function if exists app_config_bump_version();
drop table if exists reactions;
drop table if exists video_upload_sessions;
drop table if exists videos;
drop table if exists categories;
drop table if exists sessions;
drop table if exists users;
-- app_config_entries phải bỏ TRƯỚC app_config vì có khoá ngoại trỏ sang.
drop table if exists app_config_entries;
drop table if exists app_config;
drop type if exists reaction_type;

-- ---------- 2.1 Users, sessions ----------

create table users (
  id            uuid primary key default gen_random_uuid(),
  username      text not null unique,
  display_name  text not null,
  avatar_url    text,
  password_hash text not null,             -- bcrypt. Không bao giờ lưu plaintext
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

-- ---------- 2.2 Nội dung ----------

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

  -- LƯU Ý (khác SPEC một chút, có chủ đích): lưu PATH tương đối, vd
  --   /video/upload/sp_auto/<id>.m3u8
  -- API sẽ ghép base URL của request để trả absolute URL cho client.
  -- Lý do: host thay đổi liên tục giữa localhost / IP LAN / tunnel (xem SERVING.md);
  -- lưu absolute URL sẽ phải re-seed mỗi lần đổi host.
  playback_url  text not null,
  thumbnail_url text,
  status        text not null default 'READY'
                check (status in ('UPLOADING','PROCESSING','READY','FAILED','DELETED')),

  -- Counter denormalize, do trigger ở 4.4 cập nhật.
  like_count     int not null default 0,
  dislike_count  int not null default 0,
  bookmark_count int not null default 0,

  created_at    timestamptz not null default now()
);
-- Cố ý không đánh index cho query feed: `order by random()` phải quét và sort toàn bộ tập
-- `status = 'READY'`, nên không index nào giúp được. Ở pool 200 row thì việc đó là miễn phí.

-- Một row đại diện cho toàn bộ quá trình upload một video. Các part đã nhận nằm trên disk,
-- không tạo thêm table/row cho từng part.
create table video_upload_sessions (
  id         uuid primary key,
  video_id   text not null unique references videos(id) on delete cascade,
  file_size  bigint not null check (file_size > 0),
  part_size  integer not null check (part_size > 0),
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
);

-- ---------- 2.3 Reactions ----------

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

-- ---------- 2.4 Config ----------

-- Bundle config. Payload KHÔNG nằm ở đây nữa mà tách thành từng row key-value bên dưới,
-- để sửa một setting không phải ghi đè cả cục JSON.
create table app_config (
  id         serial primary key,
  version    bigint not null,
  enabled    boolean not null default false,
  updated_at timestamptz not null default now()
);

-- Đúng một bundle live tại một thời điểm.
create unique index app_config_one_live on app_config (enabled) where enabled;

-- Mỗi setting một row. key là đường dẫn chấm ("ranking.weights.likedChannel"),
-- value là jsonb để giữ nguyên kiểu (int/float/bool/array) - xem backend/config_payload.py.
create table app_config_entries (
  config_id int   not null references app_config(id) on delete cascade,
  key       text  not null,
  value     jsonb not null,
  primary key (config_id, key)
);

-- SPEC mục 5: "version phải tăng mỗi lần đổi payload". Trước đây payload là một cột nên
-- việc đó do người sửa tự nhớ; giờ payload rải ra nhiều row, quên bump version là client
-- vẫn thấy ETag cũ và giữ config cũ tới hết TTL. Đẩy hẳn cho database lo.
create or replace function app_config_bump_version() returns trigger as $$
declare target int;
begin
  target := coalesce(NEW.config_id, OLD.config_id);
  -- `now()` là thời điểm bắt đầu TRANSACTION nên cố định trong suốt transaction. Sau row đầu
  -- tiên, updated_at đã bằng now() nên điều kiện dưới sai -> các row còn lại của cùng một
  -- transaction không bump nữa. Nhờ vậy ghi 21 setting một lượt chỉ tăng version đúng 1 lần.
  update app_config
     set version = version + 1, updated_at = now()
   where id = target and updated_at < now();
  return null;
end;
$$ language plpgsql;

create trigger app_config_entries_bump
after insert or update or delete on app_config_entries
for each row execute function app_config_bump_version();

-- ---------- 4.4 Trigger counter ----------

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
