#!/usr/bin/env python3
# backend/scripts/diag_ingest.py
"""
Ingest dry-run 診斷腳本（不寫 DB）
─────────────────────────────────
用法：
    cd /Users/chenguanjun/Desktop/Python程序開發/CellTrail/backend
    source .venv/bin/activate
    python scripts/diag_ingest.py "/path/to/0801-0903彭奕翔網路歷程.xlsx"

它會：
  1. 用 ingest 的 _iter_rows_excel 讀檔
  2. 印出讀進來的「原始欄名」清單
  3. 對每個原始欄名跑 _canon → 查 carrier_profile header_map → 印對照結果
  4. 取前 3 列實際 row，印出 normalize 前後的值
  5. 模擬 ingest 主迴圈，統計 inserted / skipped / 各種 skip 原因

不依賴 DB（fallback 到 _RAW2CANON）；本腳本完全不寫任何 row。
"""
import sys
import os
import pathlib

# 把 backend 目錄加到 sys.path 以便 import app.*
HERE = pathlib.Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

# 必要的環境變數（避免 db.session 在 import 期 raise）
os.environ.setdefault("DATABASE_URL", "postgresql://celltrail:celltrail@localhost:5432/celltrail")
os.environ.setdefault("SECRET_KEY", "diag-only-not-for-prod")
os.environ.setdefault("AUTH_ENABLED", "true")


def main(xlsx_path: str) -> None:
    print(f"\n=== 診斷檔案：{xlsx_path} ===\n")
    if not pathlib.Path(xlsx_path).exists():
        print(f"❌ 找不到檔案：{xlsx_path}")
        sys.exit(2)

    file_bytes = pathlib.Path(xlsx_path).read_bytes()
    print(f"檔案大小：{len(file_bytes):,} bytes\n")

    # 1) 取 header_map（會從 DB 讀；DB 不可用會 fallback 到 _RAW2CANON）
    from app.services.carrier_profile import get_active_header_map, _canon
    try:
        header_map = get_active_header_map()
    except Exception as e:
        print(f"⚠️  carrier_profile 載入錯誤：{e}")
        from app.services.ingest import _RAW2CANON
        header_map = {_canon(k): v for k, v in _RAW2CANON.items()}
    print(f"[header_map] 已載入別名數 = {len(header_map)}\n")

    # 2) 用 ingest 的同一支 reader 讀檔
    from app.services.ingest import _iter_rows_excel
    rows = list(_iter_rows_excel(file_bytes))
    print(f"[reader] 讀出 row 數 = {len(rows)}")
    if not rows:
        print("❌ 一筆都沒讀到 — 八成是表頭偵測失敗或檔案結構特殊（例如多 sheet / 表頭埋在第 5 行）")
        sys.exit(0)

    # 3) 印第 0 列的原始欄名清單，並對每個欄名查 _canon → 對照
    print("\n[欄名對照] 第 0 列實際讀到的欄名 → _canon 後 → 對應 canonical：")
    print("-" * 78)
    raw_keys = list(rows[0].keys())
    matched, unmatched = [], []
    for k in raw_keys:
        canon_k = _canon(k)
        target = header_map.get(canon_k)
        flag = "✓" if target else " "
        line = f"  {flag} {k!r:30s} → canon={canon_k!r:25s} → {target or '(無對照，會被丟)'}"
        print(line)
        (matched if target else unmatched).append(k)
    print("-" * 78)
    print(f"  共 {len(raw_keys)} 個原始欄，命中 {len(matched)}，丟棄 {len(unmatched)}")

    # 4) 印前 3 列 normalize 後的結果
    print("\n[前 3 列 normalize 結果]")
    from app.services.ingest import _normalize_row
    for i, raw in enumerate(rows[:3], start=1):
        norm = _normalize_row(raw)
        # 同時顯示原始與正規化
        print(f"\n  Row {i} 原始:")
        for k, v in raw.items():
            print(f"    {k!r:25s} = {repr(v)[:60]}")
        print(f"  Row {i} 正規化:")
        if not norm:
            print("    (空 dict — 完全沒命中任何 header_map)")
        for k, v in norm.items():
            print(f"    {k!r:15s} = {repr(v)[:60]}")

    # 5) 模擬 ingest 主迴圈（不寫 DB）
    print("\n[模擬 ingest 主迴圈]")
    from app.services.ingest import _parse_ts, _to_int, _to_float, _guess_accuracy

    total = inserted = skipped = 0
    skip_reasons = {"no_start_ts": 0, "no_addr_no_cell": 0}
    sample_errors = []
    sample_inserts = []

    for idx, raw in enumerate(rows, start=1):
        total += 1
        r = _normalize_row(raw)
        start_ts = _parse_ts(r.get("start_ts"))
        end_ts = _parse_ts(r.get("end_ts")) or start_ts
        if not start_ts:
            skipped += 1
            skip_reasons["no_start_ts"] += 1
            if len(sample_errors) < 3:
                sample_errors.append(f"row{idx}(no_start_ts): r.start_ts={r.get('start_ts')!r}")
            continue
        cell_id = (str(r.get("cell_id") or "").strip() or None)
        cell_addr = (str(r.get("cell_addr") or "").strip() or None)
        if not cell_addr and not cell_id:
            skipped += 1
            skip_reasons["no_addr_no_cell"] += 1
            if len(sample_errors) < 3:
                sample_errors.append(f"row{idx}(no_addr_no_cell): r={r}")
            continue
        inserted += 1
        if len(sample_inserts) < 3:
            sample_inserts.append(
                f"row{idx}: start_ts={start_ts} cell_id={cell_id} cell_addr={cell_addr!r}"
            )

    print(f"  total    = {total}")
    print(f"  inserted = {inserted} (這些會走到 geocode → DB)")
    print(f"  skipped  = {skipped}")
    for reason, n in skip_reasons.items():
        print(f"    └─ {reason}: {n}")
    if sample_errors:
        print("\n  [skip 原因樣本]")
        for s in sample_errors:
            print(f"    {s}")
    if sample_inserts:
        print("\n  [insert 樣本]")
        for s in sample_inserts:
            print(f"    {s}")

    # 6) 結論建議
    print("\n=== 診斷結論 ===")
    if inserted == total:
        print("✓ 所有 row 都能 ingest 寫入。地圖看不到資料 → 問題在 geocode 階段（API key 或地址清洗）")
    elif inserted == 0:
        if skip_reasons["no_start_ts"] == total:
            print("❌ H2：所有 row 都沒抓到 start_ts。可能原因：")
            print("   1) 時間欄名沒命中 header_map（看上面欄名對照清單有沒有 ✓ 在時間欄）")
            print("   2) 時間欄命中了但值是 pandas.Timestamp 又被 _parse_ts 退回")
        elif skip_reasons["no_addr_no_cell"] == total:
            print("❌ H1：所有 row 都缺地址與 cell_id。可能原因：")
            print("   1) 地址欄與 cell_id 欄都沒命中 header_map")
            print("   2) 命中了但值都是空字串")
        else:
            print("❌ 混合原因，看上面 skip_reasons 拆解")
    else:
        print(f"⚠️  部分成功：{inserted}/{total} 可寫入。")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法：python scripts/diag_ingest.py /path/to/file.xlsx")
        sys.exit(1)
    main(sys.argv[1])
