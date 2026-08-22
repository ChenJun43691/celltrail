#!/usr/bin/env python3
# backend/scripts/probe_cell_towers.py
# ---------------------------------------------------------------------------
# 驗證 cell_towers 對照表是否真的生效 —— 不需要任何憑證。
#
# 為什麼需要這支：
#   /api/cell-towers/stats 需要 admin token，但「表裡到底有沒有這批 cell_id」
#   這件事，其實可以用訪客端點間接、且更貼近真實使用情境地測出來。
#
# 原理：
#   geocode 的查詢順序是 cell_towers → geocode_cache → OSM → 放棄
#   （見 services/geocode.py lookup / lookup_bulk）。
#   若送出一個「只有時間 + 基地台編號、完全沒有地址欄」的檔案，
#   後面幾層全都無從施力 —— 有座標回來，就只可能來自 cell_towers。
#   於是「定位成功」與「表裡有這筆」互為充要條件。
#
# 額外把關（對應 CLAUDE.md 五-X）：
#   帶 --expect 時會逐筆比對回傳座標與 CSV 中的期望值。
#   這能抓出「匯進去了、但經緯度對調 / 讀錯欄」這類最危險的情況 ——
#   那種錯誤在地圖上看起來完全正常，不主動比對就永遠不會發現。
#
# 用法：
#   python scripts/probe_cell_towers.py --expect ../data/towers.csv --sample 30
#   python scripts/probe_cell_towers.py --ids 46601198530720128011,466970821011112
#   python scripts/probe_cell_towers.py --expect towers.csv --api http://localhost:8000
#
# 注意：parse-only 限 20 req/hr/IP。本腳本**固定只送一次請求**（多筆 cell_id
#       塞在同一個檔裡），請不要包在迴圈中反覆呼叫。
#
# 零外部依賴（純 stdlib），不需啟動 venv。
# ---------------------------------------------------------------------------
import argparse
import csv
import io
import json
import random
import sys
import urllib.error
import urllib.request
import uuid

DEFAULT_API = "https://celltrail-api.onrender.com"

# 對照組：格式合法但不可能存在的編號。它必須「定位失敗」，
# 否則代表這支探針本身失去鑑別力（例如後端改成回傳預設座標）。
CONTROL_ID = "99999999999999999999"


def build_probe_csv(cell_ids):
    """組出探針檔：只有『時間』與『基地台編號』兩欄，刻意不給地址。

    欄名取自 _RAW2CANON 的 canonical alias，兩個都命中才過得了
    header detection 的 MIN_HEADER_MATCHES（≥2）門檻。
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["時間", "基地台編號"])
    for i, cid in enumerate(cell_ids):
        # 時間本身不影響定位，只是讓該列通過有效性驗證。
        w.writerow([f"2026-01-01 {i // 60:02d}:{i % 60:02d}:00", cid])
    return buf.getvalue().encode("utf-8")


def post_parse_only(api, payload, target_id):
    """以 multipart/form-data 打 /api/parse-only（stdlib 手工組 body）。"""
    boundary = f"----celltrail{uuid.uuid4().hex}"
    parts = []
    for name, value in (("target_id", target_id),):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"probe.csv\"\r\n"
        f"Content-Type: text/csv\r\n\r\n".encode()
    )
    parts.append(payload)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        f"{api.rstrip('/')}/api/parse-only",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        return e.code, {"_error": detail}


def load_expected(path):
    """讀 cell_id,lat,lng[,memo] 格式的對照表（即 geocode_verify.py 的產出）。"""
    with open(path, "rb") as f:
        rows = list(csv.reader(io.StringIO(f.read().decode("utf-8-sig"))))
    if not rows:
        sys.exit(f"✗ {path} 是空檔")
    hdr = [c.strip().lower() for c in rows[0]]
    if "cell_id" not in hdr:
        sys.exit(f"✗ {path} 首行不含 cell_id 欄，無法判讀：{hdr}")
    i_id, i_lat, i_lng = hdr.index("cell_id"), hdr.index("lat"), hdr.index("lng")
    out = {}
    for r in rows[1:]:
        if not r or not r[i_id].strip():
            continue
        out[r[i_id].strip()] = (float(r[i_lat]), float(r[i_lng]))
    return out


def main():
    ap = argparse.ArgumentParser(description="不需憑證驗證 cell_towers 是否生效")
    ap.add_argument("--expect", help="期望座標對照表 CSV（cell_id,lat,lng）；會逐筆比對座標")
    ap.add_argument("--ids", help="直接指定 cell_id，逗號分隔（與 --expect 擇一或併用）")
    ap.add_argument("--sample", type=int, default=25, help="從 --expect 抽樣幾筆（預設 25；0 = 全部）")
    ap.add_argument("--api", default=DEFAULT_API, help=f"API base（預設 {DEFAULT_API}）")
    ap.add_argument("--tolerance", type=float, default=1e-4,
                    help="座標比對容差（度，預設 1e-4 ≈ 11 公尺）")
    args = ap.parse_args()

    expected = load_expected(args.expect) if args.expect else {}
    ids = []
    if args.ids:
        ids += [x.strip() for x in args.ids.split(",") if x.strip()]
    if expected:
        pool = sorted(expected)
        if args.sample and args.sample < len(pool):
            # 固定亂數種子：同一份表重跑會抽到同一組，結果可重現、可比對。
            random.Random(42).shuffle(pool)
            pool = pool[: args.sample]
        ids += [c for c in pool if c not in ids]
    if not ids:
        sys.exit("✗ 請提供 --expect 或 --ids")

    probe_ids = ids + [CONTROL_ID]
    print(f"→ API   : {args.api}")
    print(f"→ 探測  : {len(ids)} 個 cell_id（＋1 個對照組），單一請求")

    status, data = post_parse_only(args.api, build_probe_csv(probe_ids), "_probe_cell_towers")
    if status != 200:
        sys.exit(f"✗ HTTP {status}：{data.get('_error', data)}")

    recs = {r["cell_id"]: r for r in data.get("_records", [])}
    located, missing, mismatched = [], [], []
    for cid in ids:
        r = recs.get(cid)
        if not r or r.get("lat") is None:
            missing.append(cid)
            continue
        got = (r["lat"], r["lng"])
        exp = expected.get(cid)
        if exp and (abs(got[0] - exp[0]) > args.tolerance or abs(got[1] - exp[1]) > args.tolerance):
            mismatched.append((cid, exp, got))
        else:
            located.append((cid, got))

    ctrl = recs.get(CONTROL_ID)
    ctrl_ok = ctrl is not None and ctrl.get("lat") is None

    print()
    print(f"  已定位   : {len(located)} / {len(ids)}")
    print(f"  未定位   : {len(missing)} / {len(ids)}")
    if expected:
        print(f"  座標不符 : {len(mismatched)} / {len(ids)}")
    print(f"  對照組   : {'✓ 正確未定位（探針有鑑別力）' if ctrl_ok else '✗ 竟然定位成功 —— 探針失效，結果不可信'}")

    if mismatched:
        # 這一段最重要：匯進去了但座標錯，比完全沒匯還危險。
        print("\n  ⚠ 座標與對照表不符（可能經緯度對調或讀錯欄，見 CLAUDE.md 五-X）：")
        for cid, exp, got in mismatched[:10]:
            print(f"    {cid}  期望 {exp[0]:.6f},{exp[1]:.6f}  實得 {got[0]:.6f},{got[1]:.6f}")
    if missing:
        print(f"\n  未定位範例：{', '.join(missing[:5])}")

    print()
    if not ctrl_ok:
        sys.exit(2)
    if mismatched:
        print("✗ 表已生效，但有座標對不上 —— 請勿採用，先查匯入欄位對應。")
        sys.exit(1)
    if not located:
        print("✗ 沒有任何一筆定位成功 —— 這批 cell_id 不在線上 cell_towers 中。")
        sys.exit(1)
    if missing:
        print(f"△ 部分生效：{len(located)} 筆可定位、{len(missing)} 筆仍缺。")
        sys.exit(0)
    print("✓ 全部命中且座標相符 —— cell_towers 已生效。")


if __name__ == "__main__":
    main()
