# backend/app/services/ingest.py
# ------------------------------------------------------------
# CellTrail Ingest Service
# - 支援 CSV / TXT / XLSX / PDF
# - PDF 直接在後端解析（手機用戶無需先轉檔）
# - 欄位對照：時間、地址、cell_id、方位角等
# - 缺值/#N/A 正規化、時間標準化 (UTC+8)
# - 先以 cell_id 查站點字典，失敗再用地址地理編碼
# - DB 寫入時自動帶入 geom；避免 server-side prepared 造成錯誤
# ------------------------------------------------------------

from __future__ import annotations
import csv, io, re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Iterable, Tuple

from fastapi import HTTPException
from app.db.session import get_conn
from app.services import geocode

# ====== 共用常數與工具 ======
NA_TOKENS = {"#N/A", "", "NA", "N/A", None}
TPE_TZ = timezone(timedelta(hours=8))  # Asia/Taipei

def _is_na(v):
    return v is None or (isinstance(v, str) and v.strip() in NA_TOKENS)

def _parse_ts(s: Any) -> Optional[datetime]:
    """
    支援多種時間表示，回傳含台北時區的 datetime：
      - 2025/8/30 13:31         （PDF 漫遊紀錄常見：單位數月日）
      - 2025/08/30 13:31:22     （CSV 標準格式）
      - 2024-09-01 20:06:44     （Excel 網路歷程：dash 連字符）
      - 2024-09-01\xa020:06:44  （Excel 拷貝出來常帶不間斷空格 NBSP）
      - 中文「年月日時分秒」夾雜（最後備援）
    """
    if _is_na(s):
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=TPE_TZ)
    # NBSP（不間斷空格 \xa0）→ 一般空格；前後 strip
    s = str(s).strip().replace("\xa0", " ")
    # 把多重空白壓成單一空白（Excel 偶爾會有兩個空格）
    s = re.sub(r"[ \t]+", " ", s)
    for fmt in (
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%-m/%-d %H:%M",
        "%Y-%m-%d %H:%M:%S",  # 「網路歷程.xltx」格式
        "%Y-%m-%d %H:%M",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=TPE_TZ)
        except Exception:
            continue
    # 嘗試去除多餘空白或中文標點
    s2 = re.sub(r"[年月日時分秒]", " ", s)
    s2 = re.sub(r"\s+", " ", s2).strip(" /-:")
    for fmt in ("%Y %m %d %H %M %S", "%Y %m %d %H %M"):
        try:
            dt = datetime.strptime(s2, fmt)
            return dt.replace(tzinfo=TPE_TZ)
        except Exception:
            pass
    return None

def _to_int(s: Any) -> Optional[int]:
    if _is_na(s):
        return None
    s = re.sub(r"[^\d-]", "", str(s).strip())
    if not s:
        return None
    try:
        return int(s)
    except Exception:
        return None

def _to_float(s: Any) -> Optional[float]:
    if _is_na(s):
        return None
    try:
        return float(str(s).strip())
    except Exception:
        return None

def _guess_accuracy(addr: str | None) -> int:
    """簡單估計誤差圈：市區較小、郊區較大"""
    a = (addr or "")
    if ("市" in a) or ("區" in a):
        return 150
    if ("鄉" in a) or ("村" in a):
        return 800
    return 300

# ---- 欄位名標準化 ----
def _canon(s: str) -> str:
    """標準化字串：全形轉半形、移除空白/標點、繁簡（臺→台）、小寫化"""
    if s is None:
        return ""
    s = str(s)
    try:
        import unicodedata
        s = unicodedata.normalize("NFKC", s)
    except Exception:
        pass
    s = s.replace("臺", "台")
    s = re.sub(r"[\s\u3000]+", "", s)  # 各種空白
    s = re.sub(r"[•·．\.\-:：;；,/，、\t]", "", s)  # 常見標點
    return s.lower()

# ====== 讀取 CSV / Excel ======
def _iter_rows_csv(file_bytes: bytes) -> Iterable[Dict[str, Any]]:
    text = file_bytes.decode("utf-8-sig", errors="ignore")
    rdr = csv.DictReader(io.StringIO(text))
    for r in rdr:
        yield {(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in (r or {}).items()}

def _iter_rows_excel(file_bytes: bytes) -> Iterable[Dict[str, Any]]:
    """
    讀取 .xlsx / .xltx / .xlsm / .xltm 為 row dict。

    處理電信公司常見的「假表頭」格式：
      Excel 第 0 列：跨欄合併儲存格的大標（如「行動上網」「網路歷程」）
      Excel 第 1 列：才是真正的欄位名（啟始時間 / 結束時間 / 基地台位址 ...）
      Excel 第 2 列起：才是資料

    pandas 預設 header=0 會把第 0 列當欄位名，導致除了第一欄外其餘變成
    'Unnamed: 1' ~ 'Unnamed: N'。偵測規則：當 Unnamed 佔多數欄時，重讀為 header=1。

    型別處理盲點：
      - pandas.Timestamp / numpy.datetime64 → 用 .to_pydatetime() 轉成 python datetime
        （切忌用 .item()，部分版本會回 int [ns]，後續 _parse_ts 全部失敗）
      - 其他 numpy 數值型別才用 .item() 轉原生
    """
    try:
        import pandas as pd
        import numpy as np
    except Exception as e:
        raise RuntimeError("請先安裝：pip install pandas openpyxl") from e

    # 第一輪：預設 header=0 讀
    df = pd.read_excel(io.BytesIO(file_bytes))
    # 偵測「假表頭」：Unnamed 欄位佔多數 → 真正欄位名在第 1 列
    n_cols = len(df.columns)
    n_unnamed = sum(1 for c in df.columns if str(c).startswith("Unnamed:"))
    if n_cols >= 3 and n_unnamed >= max(2, n_cols // 2):
        df = pd.read_excel(io.BytesIO(file_bytes), header=1)

    df = df.replace({np.nan: ""})
    for _, row in df.iterrows():
        d = {str(k).strip(): row[k] for k in df.columns}
        for k, v in list(d.items()):
            # pandas.Timestamp / numpy.datetime64 → python datetime（保留時區邏輯由 _parse_ts 處理）
            if hasattr(v, "to_pydatetime"):
                try:
                    d[k] = v.to_pydatetime()
                    continue
                except Exception:
                    pass
            # 其他 numpy 型別 → python 原生（跳過 str/bytes，避免它們意外實作了 .item()）
            try:
                if hasattr(v, "item") and not isinstance(v, (str, bytes)):
                    d[k] = v.item()
            except Exception:
                pass
        yield d

# ====== 欄位對照（來源→系統） ======
# ----------------------------------------------------------------------
# W1 重構（2026-04-27）：
#   原本 _RAW2CANON 是熱路徑用的 source of truth。重構後：
#     - DB 表 carrier_profiles 是新的 SoT；ingest 透過 carrier_profile service 取
#     - 此處 _RAW2CANON 仍保留，但**降級為「DB 不可用時的 fallback 種子」**，
#       同時是單元測試的對照基準（保證 DB 種子與 code fallback 不會飄離）
#
# 加 / 改別名的標準流程（從這個版本起）：
#   1. 直接在 carrier_profiles 表新增 / 修改 mapping_json（Web admin 介面或 SQL）
#   2. 呼叫 carrier_profile.invalidate_cache() 讓 worker 立刻生效
#   3. 不需要改本檔、不需要 deploy
# ----------------------------------------------------------------------
_RAW2CANON = {
    # 時間
    "開始連線時間": "start_ts",
    "結束連線時間": "end_ts",
    "開始時間": "start_ts",
    "結束時間": "end_ts",
    "起始時間": "start_ts",
    "啟始時間": "start_ts",     # 「網路歷程.xltx」用「啟」非「起」
    "終止時間": "end_ts",
    "時間": "start_ts",          # W1 新增：「0801-0903彭奕翔網路歷程.xlsx」
    "始話時間": "start_ts",      # W1 新增：「電話通聯+歷程.xlsx」
    "通聯時間": "start_ts",      # W1 新增：常見通聯紀錄欄名
    # 基地台/地址/識別
    "基地台地址": "cell_addr",
    "基地臺地址": "cell_addr",
    "基地台位址": "cell_addr",   # 「網路歷程.xltx」用「位址」非「地址」
    "基地臺位址": "cell_addr",
    "最終基地台位址": "cell_addr",
    "最終基地臺位址": "cell_addr",
    "站台地址": "cell_addr",
    "地址": "cell_addr",
    "起址": "cell_addr",          # W1 新增：「周蔓達上網歷程.xlsx」起話端位址
    "基地台編號": "cell_id",
    "基地臺編號": "cell_id",
    "基地台ID": "cell_id",       # Excel 範本有時用 ID 而非「編號」
    "基地臺ID": "cell_id",
    "最終基地台ID": "cell_id",
    "最終基地臺ID": "cell_id",
    "站台編號": "cell_id",
    "站碼": "cell_id",
    "cell_id": "cell_id",
    "基地台": "cell_id",          # W1 新增：「0801-0903彭奕翔網路歷程.xlsx」
    "基地台/交換機": "cell_id",   # W1 新增：「電話通聯+歷程.xlsx」
    "起台": "cell_id",            # W1 新增：「周蔓達上網歷程.xlsx」起話端
    # 其他
    "細胞名稱": "sector_name",
    "小區名稱": "sector_name",
    "台號": "site_code",
    "站號": "site_code",
    "站名": "site_code",
    "細胞": "sector_id",
    "小區": "sector_id",
    "cell": "sector_id",
    "方位": "azimuth",
    "方位角": "azimuth",
    "azimuth": "azimuth",
}
# 注意：HEADER_MAP 保留為 module-level 物件以維持向後相容
# （任何外部模組若直接 import HEADER_MAP 不會壞），
# 但實際 _normalize_row 已不使用此常數，改走 carrier_profile service
HEADER_MAP = {_canon(k): v for k, v in _RAW2CANON.items()}


def _normalize_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """
    把原始 row dict（來源欄名）正規化成 canonical row dict。
    W1 起改從 carrier_profile service 取對照表（DB 為 SoT），
    DB 不可用時 service 會自動 fallback 到本檔的 _RAW2CANON。
    """
    # lazy import 避免 circular（service 也可能 import ingest 做 fallback）
    from app.services.carrier_profile import get_active_header_map
    header_map = get_active_header_map()
    out: Dict[str, Any] = {}
    for k, v in (r or {}).items():
        key = header_map.get(_canon(k))
        if key:
            out[key] = v
    return out

# ====== DB 寫入（避免 prepared statement 衝突） ======
def _insert_records(records: List[Dict[str, Any]]) -> int:
    """批次寫入 raw_traces；座標自帶 geom"""
    if not records:
        return 0
    sql = """
    INSERT INTO raw_traces (
      project_id, target_id, start_ts, end_ts,
      cell_id, cell_addr, sector_name, site_code, sector_id,
      azimuth, lat, lng, accuracy_m, geom
    )
    VALUES (
      %(project_id)s::text, %(target_id)s::text,
      %(start_ts)s::timestamptz, %(end_ts)s::timestamptz,
      %(cell_id)s::text, %(cell_addr)s::text, %(sector_name)s::text, %(site_code)s::text, %(sector_id)s::text,
      %(azimuth)s::int, %(lat)s::float8, %(lng)s::float8, %(accuracy_m)s::int,
      CASE
        WHEN %(lat)s::float8 IS NOT NULL AND %(lng)s::float8 IS NOT NULL
          THEN ST_SetSRID(ST_MakePoint(%(lng)s::float8, %(lat)s::float8), 4326)
        ELSE NULL
      END
    )
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # psycopg3 預設多次執行後可能 server-side prepare；
                # 這裡用 executemany 但不手動 prepare，並仰賴 get_conn() 裡的 prepare_threshold=0（已在 session.py 設定）
                cur.executemany(sql, records)
        return len(records)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"匯入失敗：{type(e).__name__}: {e}")

# ====== 主要匯入（自動判斷副檔名；PDF 直接支援） ======
def ingest_auto(project_id: str, target_id: str, filename: str, file_bytes: bytes) -> Dict[str, Any]:
    """
    前端一律呼叫這支：依副檔名自動分流
    - .csv/.txt/.tsv → 文字表格
    - .xlsx → Excel
    - .pdf → 走 PDF 解析（手機拍來的 PDF 也可）
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in {"csv", "txt", "tsv"}:
        return _ingest_rows_stream(project_id, target_id, _iter_rows_csv(file_bytes))
    # xlsx / xltx（Excel 範本）/ xlsm（含巨集）/ xltm（含巨集範本）皆走同一條 openpyxl 路徑
    elif ext in {"xlsx", "xltx", "xlsm", "xltm"}:
        return _ingest_rows_stream(project_id, target_id, _iter_rows_excel(file_bytes))
    elif ext == "pdf":
        return ingest_pdf(project_id, target_id, file_bytes)
    else:
        raise ValueError("不支援的檔案格式：請使用 CSV / TXT / XLSX / XLTX / PDF")

def _ingest_rows_stream(project_id: str, target_id: str, rows_iter: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    total = inserted = skipped = 0
    errors: List[str] = []
    to_insert: List[Dict[str, Any]] = []

    for idx, raw in enumerate(rows_iter, start=1):
        total += 1
        r = _normalize_row(raw)

        start_ts = _parse_ts(r.get("start_ts"))
        end_ts = _parse_ts(r.get("end_ts")) or start_ts
        if not start_ts:
            skipped += 1
            errors.append(f"row{idx}: 缺少開始連線時間")
            continue

        cell_id = (str(r.get("cell_id") or "").strip() or None)
        cell_addr = (str(r.get("cell_addr") or "").strip() or None)
        sector_name = (str(r.get("sector_name") or "").strip() or None)
        site_code = (str(r.get("site_code") or "").strip() or None)
        sector_id = (str(r.get("sector_id") or "").strip() or None)
        azimuth = _to_int(r.get("azimuth"))

        if not cell_addr and not cell_id:
            skipped += 1
            errors.append(f"row{idx}: 地址與 cell_id 皆空，無法定位")
            continue

        lat = lng = None
        try:
            ll = geocode.lookup(cell_id, cell_addr)
            if ll:
                lat, lng = ll
        except Exception as e:
            errors.append(f"row{idx}: geocode 失敗：{e}")

        accuracy_m = _guess_accuracy(cell_addr)

        to_insert.append(
            dict(
                project_id=project_id,
                target_id=target_id,
                start_ts=start_ts,
                end_ts=end_ts,
                cell_id=cell_id,
                cell_addr=cell_addr,
                sector_name=sector_name,
                site_code=site_code,
                sector_id=sector_id,
                azimuth=azimuth,
                lat=_to_float(lat),
                lng=_to_float(lng),
                accuracy_m=_to_int(accuracy_m),
            )
        )

    if to_insert:
        inserted = _insert_records(to_insert)

    return {"total": total, "inserted": inserted, "skipped": skipped, "errors": errors[:50]}

# ====== PDF 匯入（不需先轉檔） ======
# pdfplumber 為可選依賴，缺少時給清楚訊息
try:
    import pdfplumber  # type: ignore
except Exception:  # pragma: no cover
    pdfplumber = None

def _match_col_idx(headers: List[str]) -> Dict[str, int]:
    """以標準化表頭來找出欄位位置；找不到回傳 -1。"""
    h = [_canon(x) for x in headers]
    cands = {
        "start": [_canon(x) for x in ["開始連線時間", "開始時間", "起始時間"]],
        "end":   [_canon(x) for x in ["結束連線時間", "結束時間", "終止時間"]],
        "cellid":[_canon(x) for x in ["基地台編號", "基地臺編號", "站台編號", "站碼", "cell_id"]],
        "addr":  [_canon(x) for x in ["基地台地址", "基地臺地址", "站台地址", "地址"]],
        "sector":[_canon(x) for x in ["細胞名稱", "小區名稱"]],
        "site":  [_canon(x) for x in ["台號", "站號", "站名"]],
        "cid":   [_canon(x) for x in ["細胞", "小區", "cell"]],
        "az":    [_canon(x) for x in ["方位", "方位角", "azimuth"]],
    }
    got: Dict[str, int] = {}
    for key, cs in cands.items():
        idx = -1
        for i, name in enumerate(h):
            if any(c in name for c in cs):
                idx = i
                break
        got[key] = idx
    return got

def _split_columns_fallback(lines: List[str]) -> List[List[str]]:
    """無表格線時，以多空白或 tab 嘗試切欄"""
    splitter = r"(?:\s{2,}|\t+)"
    rows = []
    for ln in lines:
        cols = [s for s in re.split(splitter, ln) if s.strip()]
        rows.append(cols)
    return rows

def _extract_tables_from_page(page) -> List[List[List[str]]]:
    """先用表格線策略，失敗再文字行距拆欄"""
    tables: List[List[List[str]]] = []
    try:
        tables = page.extract_tables(
            {
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
                "snap_tolerance": 3,
                "intersection_x_tolerance": 5,
                "intersection_y_tolerance": 5,
            }
        ) or []
    except Exception:
        tables = []
    if not tables:
        try:
            text = page.extract_text() or ""
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if lines:
                # 找到第一行表頭（含任一已知欄名）
                header_idx = -1
                for i, ln in enumerate(lines):
                    canon_ln = _canon(ln)
                    if any(_canon(k) in canon_ln for k in _RAW2CANON.keys()):
                        header_idx = i
                        break
                if header_idx >= 0:
                    hdr = _split_columns_fallback([lines[header_idx]])[0]
                    body = _split_columns_fallback(lines[header_idx + 1 :])
                    tables = [[hdr, *body]]
        except Exception:
            pass
    return tables

def ingest_pdf(project_id: str, target_id: str, file_bytes: bytes) -> Dict[str, Any]:
    if pdfplumber is None:
        raise HTTPException(status_code=400, detail="後端缺少 pdfplumber：請安裝 pip install pdfplumber")
    total = inserted = skipped = 0
    errors: List[str] = []
    rows: List[Dict[str, Any]] = []

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            tables = _extract_tables_from_page(page)
            for t in tables:
                if not t:
                    continue
                header = [(c or "").strip() for c in t[0]]
                col = _match_col_idx(header)
                for ridx, row in enumerate(t[1:], start=2):
                    total += 1
                    rr = [(row[i] if i < len(row) else "") for i in range(len(header))]

                    start_ts = _parse_ts(rr[col["start"]]) if col.get("start", -1) >= 0 else None
                    end_ts = _parse_ts(rr[col["end"]]) if col.get("end", -1) >= 0 else start_ts
                    cell_id = (rr[col["cellid"]].strip() if col.get("cellid", -1) >= 0 and rr[col["cellid"]] else None)
                    cell_addr = (rr[col["addr"]].strip()   if col.get("addr", -1)   >= 0 and rr[col["addr"]]   else None)
                    sector_name = (
                        rr[col["sector"]].strip() if col.get("sector", -1) >= 0 and rr[col["sector"]] else None
                    )
                    site_code = (rr[col["site"]].strip()   if col.get("site", -1)   >= 0 and rr[col["site"]]   else None)
                    sector_id = (rr[col["cid"]].strip()    if col.get("cid", -1)    >= 0 and rr[col["cid"]]    else None)
                    azimuth = _to_int(rr[col["az"]]) if col.get("az", -1) >= 0 else None

                    if not start_ts:
                        skipped += 1
                        errors.append(f"page{pno} row{ridx}: 缺少開始連線時間")
                        continue
                    if not cell_addr and not cell_id:
                        skipped += 1
                        errors.append(f"page{pno} row{ridx}: 地址與 cell_id 皆空，無法定位")
                        continue

                    lat = lng = None
                    try:
                        ll = geocode.lookup(cell_id, cell_addr)
                        if ll:
                            lat, lng = ll
                    except Exception as e:
                        errors.append(f"page{pno} row{ridx}: geocode 失敗：{e}")

                    accuracy_m = _guess_accuracy(cell_addr)

                    rows.append(
                        dict(
                            project_id=project_id,
                            target_id=target_id,
                            start_ts=start_ts,
                            end_ts=end_ts,
                            cell_id=cell_id,
                            cell_addr=cell_addr,
                            sector_name=sector_name,
                            site_code=site_code,
                            sector_id=sector_id,
                            azimuth=azimuth,
                            lat=_to_float(lat),
                            lng=_to_float(lng),
                            accuracy_m=_to_int(accuracy_m),
                        )
                    )

    if rows:
        inserted = _insert_records(rows)

    return {"total": total, "inserted": inserted, "skipped": skipped, "errors": errors[:50]}