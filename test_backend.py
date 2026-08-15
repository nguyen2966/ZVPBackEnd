"""
Acceptance test cho backend - bám đúng 13 bước trong docs/SPEC.md mục 8, cộng thêm vài test
cho các bất biến mà mục 8 không kiểm trực tiếp.

Chạy (server phải đang chạy):
    python main.py                  # cửa sổ khác
    python test_backend.py
    python test_backend.py --base http://192.168.29.9:3000

Script tự dọn: mọi reaction do test tạo ra đều nằm trên user test và được ghi đè bằng LWW,
nhưng vẫn nên chạy `python -m backend.seed` lại nếu muốn DB sạch hoàn toàn.

Bộ ba quan trọng nhất là bước 5, 6, 7 (idempotency + LWW hai chiều): đó là toàn bộ cơ chế
chống đếm đôi và chống mất dữ liệu khi client retry hoặc user đổi thiết bị.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

BASE = "http://127.0.0.1:3000"
USERNAME = "khoa"
PASSWORD = "password123"

# Mốc thời gian cho các mutation, tính động để script CHẠY LẠI ĐƯỢC mà không cần re-seed.
#
# Hai ràng buộc đồng thời:
#   - phải LUÔN Ở QUÁ KHỨ so với now() của server: SPEC 4.1 kẹp client_updated_at về
#     now() + 2 phút, nên timestamp tương lai sẽ bị kẹp bằng nhau và làm hỏng thứ tự LWW.
#   - phải MỚI HƠN lần chạy trước: LWW từ chối timestamp cũ hơn giá trị đang lưu, nên mốc
#     cố định sẽ khiến lần chạy thứ hai trả STALE ở đúng chỗ đáng lẽ phải APPLIED.
# Lấy mốc = now - 1 ngày rồi cộng dồn vài giờ thoả cả hai.
_BASE_TS = datetime.now(timezone.utc) - timedelta(days=1)


def ts(hours: float) -> str:
    """Timestamp ISO-8601 UTC, lệch `hours` giờ so với mốc gốc của lần chạy này."""
    t = _BASE_TS + timedelta(hours=hours)
    return f"{t:%Y-%m-%dT%H:%M:%S}.{t.microsecond // 1000:03d}Z"

passed = 0
failed = 0
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        _failures.append(name)
        print(f"  FAIL  {name}")
        if detail:
            print(f"        {detail[:300]}")


def section(title: str) -> None:
    print(f"\n{title}")


def mutation(video_id: str, type_: str, active: bool, ts: str, mid: str | None = None) -> dict[str, Any]:
    return {
        "mutationId": mid or str(uuid.uuid4()),
        "videoId": video_id,
        "type": type_,
        "active": active,
        "clientUpdatedAt": ts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=BASE)
    args = parser.parse_args()
    base = args.base.rstrip("/")

    client = httpx.Client(base_url=base, timeout=30.0)

    # ---------------------------------------------------------------- 1. Login
    section("1. POST /api/auth/login")
    r = client.post("/api/auth/login",
                    json={"username": USERNAME, "password": PASSWORD, "deviceId": "devA"})
    check("login trả 200", r.status_code == 200, r.text)
    body = r.json()
    check("có accessToken", bool(body.get("accessToken")))
    check("có refreshToken", bool(body.get("refreshToken")))
    check("expiresInSeconds là số", isinstance(body.get("expiresInSeconds"), int))
    token_a = body["accessToken"]
    refresh_a = body["refreshToken"]
    auth_a = {"authorization": f"Bearer {token_a}"}

    check("sai mật khẩu -> 401",
          client.post("/api/auth/login",
                      json={"username": USERNAME, "password": "wrong", "deviceId": "devA"}
                      ).status_code == 401)

    # ---------------------------------------------------------------- 2. Feed
    section("2. GET /api/feed trả đúng 10 item, đủ field")
    r = client.get("/api/feed", headers=auth_a)
    check("feed trả 200", r.status_code == 200, r.text)
    items = r.json()["items"]
    check("đúng 10 item", len(items) == 10, f"got {len(items)}")
    check("không có cursor/hasMore (SPEC mục 9)",
          set(r.json().keys()) == {"items"}, str(r.json().keys()))

    v = items[0]["video"]
    for field in ("id", "user", "category", "title", "caption", "durationMs",
                  "playbackAsset", "thumbnailAsset", "engagement", "viewerState"):
        check(f"video.{field} có mặt", field in v)
    check("position 0-based liên tục", [i["position"] for i in items] == list(range(10)))
    check("video.id không rỗng", all(i["video"]["id"] for i in items))
    check("playbackAsset.url không rỗng",
          all(i["video"]["playbackAsset"]["url"] for i in items))
    check("playbackAsset.url là absolute URL",
          all(i["video"]["playbackAsset"]["url"].startswith("http") for i in items))
    check("engagement là số nguyên",
          all(isinstance(i["video"]["engagement"][k], int)
              for i in items for k in ("likeCount", "dislikeCount", "bookmarkCount")))
    check("viewerState có isBookmarked + reaction",
          all({"isBookmarked", "reaction"} <= set(i["video"]["viewerState"]) for i in items))
    check("category.name có mặt", all("name" in i["video"]["category"] for i in items))
    check("KHÔNG lồng thừa items[].video.video",
          all("video" not in i["video"] for i in items))

    check("thiếu token -> 401", client.get("/api/feed").status_code == 401)

    # ---------------------------------------------------------------- 3. Random
    section("3. Hai lần gọi feed phải khác nhau (random, không cursor)")
    seen = []
    for _ in range(5):
        seen.append([i["video"]["id"] for i in client.get("/api/feed", headers=auth_a).json()["items"]])
    check("5 lần gọi không ra cùng một tập", len({tuple(s) for s in seen}) > 1)

    # Video dùng cho các bước reaction. Lấy luôn likeCount hiện tại từ chính response feed này
    # làm mốc "before" - không cần query riêng, và đúng kể cả khi test chạy lại nhiều lần.
    target_item = items[0]["video"]
    target = target_item["id"]
    before = target_item["engagement"]["likeCount"]
    print(f"  (video test: {target}, likeCount ban đầu = {before})")

    # ---------------------------------------------------------------- 4. Like
    section("4. POST /api/reactions - LIKE một video")
    mid_like = str(uuid.uuid4())
    payload = {"mutations": [mutation(target, "LIKE", True, ts(0), mid_like)]}
    r = client.post("/api/reactions", headers=auth_a, json=payload)
    check("trả 200", r.status_code == 200, r.text)
    data = r.json()
    check("results[0].status = APPLIED", data["results"][0]["status"] == "APPLIED", json.dumps(data))
    check("mutationId phản hồi đúng", data["results"][0]["mutationId"] == mid_like)
    after = next(x["likeCount"] for x in data["videos"] if x["id"] == target)
    check("likeCount tăng 1", after == before + 1, f"{before} -> {after}")

    # ---------------------------------------------------------------- 5. Idempotency
    section("5. IDEMPOTENCY - gửi lại y nguyên request bước 4 (test quan trọng nhất)")
    r = client.post("/api/reactions", headers=auth_a, json=payload)
    data = r.json()
    check("status = STALE", data["results"][0]["status"] == "STALE", json.dumps(data))
    again = next(x["likeCount"] for x in data["videos"] if x["id"] == target)
    check("likeCount KHÔNG tăng nữa", again == after, f"{after} -> {again}")
    check("STALE kèm current", "current" in data["results"][0])
    check("current.active = true", data["results"][0]["current"]["active"] is True)

    # ---------------------------------------------------------------- 6. LWW cũ hơn
    section("6. LWW - clientUpdatedAt CŨ HƠN, active=false -> phải bị từ chối")
    r = client.post("/api/reactions", headers=auth_a, json={
        "mutations": [mutation(target, "LIKE", False, ts(-1))]
    })
    data = r.json()
    check("status = STALE", data["results"][0]["status"] == "STALE", json.dumps(data))
    still = next(x["likeCount"] for x in data["videos"] if x["id"] == target)
    check("like vẫn còn (count không đổi)", still == after, f"{after} -> {still}")

    # ---------------------------------------------------------------- 7. LWW mới hơn
    section("7. LWW - clientUpdatedAt MỚI HƠN, active=false -> phải áp dụng")
    r = client.post("/api/reactions", headers=auth_a, json={
        "mutations": [mutation(target, "LIKE", False, ts(1))]
    })
    data = r.json()
    check("status = APPLIED", data["results"][0]["status"] == "APPLIED", json.dumps(data))
    dropped = next(x["likeCount"] for x in data["videos"] if x["id"] == target)
    check("likeCount giảm 1", dropped == after - 1, f"{after} -> {dropped}")

    # ---------------------------------------------------------------- 8. Tombstone
    section("8. Tombstone không lộ ra API (bất biến 3 + 4)")
    r = client.get("/api/reactions", headers=auth_a)
    check("trả 200", r.status_code == 200)
    rows = r.json()["items"]
    check("không có entry LIKE cho video vừa bỏ",
          not any(x["videoId"] == target and x["type"] == "LIKE" for x in rows))

    # ---------------------------------------------------------------- 9. Loại trừ LIKE/DISLIKE
    section("9. Loại trừ LIKE/DISLIKE (bất biến 6)")
    client.post("/api/reactions", headers=auth_a, json={
        "mutations": [mutation(target, "LIKE", True, ts(2))]
    })
    client.post("/api/reactions", headers=auth_a, json={
        "mutations": [mutation(target, "DISLIKE", True, ts(3))]
    })
    rows = client.get("/api/reactions", headers=auth_a).json()["items"]
    for_target = {x["type"] for x in rows if x["videoId"] == target}
    check("chỉ còn DISLIKE, không thấy cả hai", for_target == {"DISLIKE"}, str(for_target))

    # ---------------------------------------------------------------- 10. Batch có item lỗi
    section("10. Batch có item lỗi -> 200, item hợp lệ vẫn được ghi (bất biến 7)")
    ok_mid, bad_mid = str(uuid.uuid4()), str(uuid.uuid4())
    r = client.post("/api/reactions", headers=auth_a, json={"mutations": [
        mutation(target, "BOOKMARK", True, ts(4), ok_mid),
        mutation("khong-ton-tai", "LIKE", True, ts(4), bad_mid),
    ]})
    check("HTTP vẫn là 200", r.status_code == 200, r.text)
    data = r.json()
    by_id = {x["mutationId"]: x for x in data["results"]}
    check("item hợp lệ APPLIED", by_id[ok_mid]["status"] == "APPLIED", json.dumps(data))
    check("item sai REJECTED", by_id[bad_mid]["status"] == "REJECTED")
    check("reason = VIDEO_NOT_FOUND", by_id[bad_mid].get("reason") == "VIDEO_NOT_FOUND")
    rows = client.get("/api/reactions", headers=auth_a).json()["items"]
    check("bookmark ĐÃ được ghi thật",
          any(x["videoId"] == target and x["type"] == "BOOKMARK" for x in rows))

    section("10b. Các reason REJECTED còn lại (SPEC 4.2)")
    r = client.post("/api/reactions", headers=auth_a, json={"mutations": [
        mutation(target, "LOVE", True, ts(4)),
        mutation(target, "LIKE", True, "khong-phai-timestamp"),
        mutation(target, "LIKE", True, "1990-01-01T00:00:00Z"),
    ]})
    res = r.json()["results"]
    check("type lạ -> INVALID_TYPE", res[0].get("reason") == "INVALID_TYPE", json.dumps(res))
    check("timestamp hỏng -> INVALID_TIMESTAMP", res[1].get("reason") == "INVALID_TIMESTAMP")
    check("timestamp lệch >1 năm -> INVALID_TIMESTAMP", res[2].get("reason") == "INVALID_TIMESTAMP")

    section("10c. Batch quá lớn -> 400 BATCH_TOO_LARGE")
    big = {"mutations": [mutation(target, "LIKE", True, ts(4)) for _ in range(201)]}
    r = client.post("/api/reactions", headers=auth_a, json=big)
    check("HTTP 400", r.status_code == 400, r.text)
    check("code = BATCH_TOO_LARGE", r.json()["error"]["code"] == "BATCH_TOO_LARGE", r.text)

    # ---------------------------------------------------------------- 13. video nhúng kèm
    section("13. GET /api/reactions nhúng kèm video đầy đủ (SPEC 3.6)")
    rows = client.get("/api/reactions", headers=auth_a).json()["items"]
    check("có ít nhất 1 row", len(rows) > 0)
    embedded = rows[0]["video"]
    for field in ("id", "title", "thumbnailUrl", "durationMs", "category", "creator", "engagement"):
        check(f"video.{field} không null", embedded.get(field) is not None, json.dumps(embedded))
    for field in ("id", "displayName", "username"):
        check(f"creator.{field} không null", embedded["creator"].get(field) is not None)

    # ---------------------------------------------------------------- 12. Config
    section("12. GET /api/config + ETag/304")
    r = client.get("/api/config", headers=auth_a)
    check("trả 200", r.status_code == 200, r.text)
    check("có ETag", "etag" in {k.lower() for k in r.headers})
    cfg = r.json()
    check("có version", isinstance(cfg.get("version"), int))
    check("có ttlSeconds", isinstance(cfg.get("ttlSeconds"), int))
    check("payload có đủ 4 nhóm key",
          {"feed", "ranking", "sync", "cache"} <= set(cfg.get("payload", {})), json.dumps(cfg)[:200])
    check("ranking.weights có mặt", "weights" in cfg["payload"]["ranking"])
    etag = r.headers["etag"]
    r304 = client.get("/api/config", headers={**auth_a, "if-none-match": etag})
    check("If-None-Match -> 304", r304.status_code == 304, f"got {r304.status_code}")

    # ---------------------------------------------------------------- 11. Một session
    section("11. Một session duy nhất (bất biến 8) - để cuối vì nó giết token devA")
    r = client.post("/api/auth/login",
                    json={"username": USERNAME, "password": PASSWORD, "deviceId": "devB"})
    check("login devB trả 200", r.status_code == 200)
    token_b = r.json()["accessToken"]

    r = client.get("/api/feed", headers=auth_a)   # token cũ của devA
    check("token devA giờ trả 401", r.status_code == 401, r.text)
    check("code = SESSION_REVOKED (không phải TOKEN_EXPIRED)",
          r.json()["error"]["code"] == "SESSION_REVOKED", r.text)
    check("shape lỗi đúng {error:{code,message}}",
          set(r.json()["error"]) >= {"code", "message"}, r.text)

    check("token devB vẫn dùng được",
          client.get("/api/feed", headers={"authorization": f"Bearer {token_b}"}).status_code == 200)

    r = client.post("/api/auth/refresh", json={"refreshToken": refresh_a})
    check("refresh của session đã revoke -> 401", r.status_code == 401)
    check("code = SESSION_REVOKED", r.json()["error"]["code"] == "SESSION_REVOKED", r.text)

    # ---------------------------------------------------------------- register
    section("14. POST /api/auth/register")
    uname = f"tester_{uuid.uuid4().hex[:8]}"
    r = client.post("/api/auth/register",
                    json={"username": uname, "password": "pw12345", "displayName": "Tester"})
    check("trả 201", r.status_code == 201, r.text)
    check("trả userId/username/displayName",
          {"userId", "username", "displayName"} <= set(r.json()), r.text)
    r = client.post("/api/auth/register",
                    json={"username": uname, "password": "pw12345", "displayName": "Tester"})
    check("trùng username -> 409", r.status_code == 409)
    check("code = USERNAME_TAKEN", r.json()["error"]["code"] == "USERNAME_TAKEN", r.text)

    # ---------------------------------------------------------------- asset thật
    section("15. playbackAsset.url phục vụ được HLS thật")
    token_b_hdr = {"authorization": f"Bearer {token_b}"}
    item = client.get("/api/feed", headers=token_b_hdr).json()["items"][0]["video"]
    r = client.get(item["playbackAsset"]["url"])
    check("master playlist trả 200", r.status_code == 200, item["playbackAsset"]["url"])
    check("là multivariant (>=3 rendition)", r.text.count("#EXT-X-STREAM-INF") >= 3, r.text[:200])
    variant = next(l.strip() for l in r.text.splitlines() if l.strip() and not l.startswith("#"))
    rv = client.get(variant)
    check("variant playlist trả 200", rv.status_code == 200)
    check("variant có #EXT-X-ENDLIST", "#EXT-X-ENDLIST" in rv.text)
    rt = client.get(item["thumbnailAsset"]["url"])
    check("thumbnail trả 200", rt.status_code == 200)

    client.close()

    print("\n" + "=" * 60)
    print(f"PASSED: {passed}   FAILED: {failed}")
    if _failures:
        print("Các test hỏng:")
        for f in _failures:
            print("  -", f)
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
