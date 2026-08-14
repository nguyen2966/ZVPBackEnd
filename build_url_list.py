"""
Pipeline: file danh sách URL (Excel .xlsx) -> urls.json

Đọc file Excel chứa danh sách link YouTube Shorts đã gom sẵn (vd: raw/batch_1.xlsx),
chuẩn hoá thành JSON gọn để dùng làm input cho dowload.py hoặc các bước khác.

Định dạng Excel đầu vào (xem raw/batch_1.xlsx):
    - Sheet đầu tiên, 3 cột: STT, Video_Url, Category
    - STT: số thứ tự (1-based)
    - Video_Url: link YouTube Shorts đầy đủ (có thể kèm ?si=...)
    - Category: do cột này được tạo từ merge cell trên Google Sheet/Excel, chỉ dòng đầu
      mỗi nhóm có giá trị, các dòng bên dưới để trống (NaN) nhưng thực ra vẫn thuộc cùng
      category với dòng có giá trị gần nhất phía trên -> phải forward-fill, KHÔNG được
      coi NaN là "không có category".

Cài đặt trước khi chạy:
    pip install pandas openpyxl

Cách chạy:
    python build_url_list.py [đường_dẫn_input.xlsx] [đường_dẫn_output.json]
    (mặc định: raw/batch_1.xlsx -> urls.json)
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("build_url_list")

DEFAULT_INPUT = Path("raw/batch_1.xlsx")
DEFAULT_OUTPUT = Path("urls.json")

REQUIRED_COLUMNS = ("STT", "Video_Url", "Category")


def load_urls_from_xlsx(path: Path) -> list[dict[str, Any]]:
    """
    Đọc file Excel danh sách URL, trả về list dict {stt, url, category} theo đúng thứ tự
    trong file. Category được forward-fill vì file gốc chỉ điền 1 lần ở đầu mỗi nhóm.
    """
    df = pd.read_excel(path, sheet_name=0)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise SystemExit(
            f"File '{path}' thiếu cột bắt buộc: {missing}. Cột hiện có: {list(df.columns)}"
        )

    df["Category"] = df["Category"].ffill()

    records: list[dict[str, Any]] = []
    for row in df.itertuples(index=False):
        url = str(row.Video_Url).strip()
        if not url or url.lower() == "nan":
            logger.warning("Bỏ qua dòng STT=%s vì thiếu Video_Url", row.STT)
            continue
        records.append({
            "stt": int(row.STT),
            "url": url,
            "category": row.Category,
        })
    return records


def main() -> None:
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUTPUT

    if not input_path.exists():
        raise SystemExit(f"Không tìm thấy file input: {input_path}")

    logger.info("Đang đọc: %s", input_path)
    records = load_urls_from_xlsx(input_path)

    output_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Đã ghi %d URL vào %s", len(records), output_path)


if __name__ == "__main__":
    main()
