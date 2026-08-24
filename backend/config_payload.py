"""
Chuyển đổi giữa payload config dạng lồng nhau (client đọc) và dạng key-value phẳng (DB lưu).

DB lưu mỗi setting một row với key dạng đường dẫn chấm:

    key                             value (jsonb)
    ------------------------------  --------------
    feed.pageSize                   10
    ranking.positiveCompletionRate  0.6
    ranking.enabled                 ["likedChannel", ...]
    ranking.weights.likedChannel    1.0

API vẫn trả về đúng shape lồng nhau ở SPEC mục 5 - client không thấy khác biệt gì.

Vì sao value là `jsonb` chứ không phải `text`:
    payload có int (10), float (0.6), bool và cả mảng (ranking.enabled). Lưu text thì phải
    tự đoán kiểu lúc đọc ra, sai một cái là client nhận `"0.6"` thay vì `0.6`. jsonb giữ
    nguyên kiểu, và mảng vẫn nằm gọn trong một row thay vì phải chẻ ra bảng con.

Giới hạn đã biết: tên key không được chứa dấu chấm, vì dấu chấm là ký tự phân cấp.
Payload ở SPEC mục 5 không có key nào như vậy.
"""

from __future__ import annotations

from typing import Any


def flatten(payload: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Payload lồng nhau -> {dotted_key: value}. List và scalar đều là lá, không đi sâu thêm."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if "." in key:
            raise ValueError(f"Key chứa dấu chấm nên không lưu phẳng được: {prefix}{key}")
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten(value, dotted))
        else:
            out[dotted] = value
    return out


def unflatten(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """{dotted_key: value} -> payload lồng nhau, dựng lại đúng shape SPEC mục 5."""
    root: dict[str, Any] = {}
    for dotted, value in pairs:
        parts = dotted.split(".")
        node = root
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                # vd vừa có key "feed" (lá) vừa có "feed.pageSize" (nhánh) -> dữ liệu hỏng
                raise ValueError(f"Key xung đột giữa lá và nhánh tại '{part}' trong '{dotted}'")
            node = child
        node[parts[-1]] = value
    return root
