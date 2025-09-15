import csv, io, re
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional

from app.db.session import pool
from app.services import geocode

NA_TOKENS = {"#N/A", "", "NA", "N/A", None}

def _is_na(v): return v is None or (isinstance(v, str) and v.strip() in NA_TOKENS)

def _parse_ts(s: str | None) -> Optional[datetime]:
    if _is_na(s): return None
    s = s.strip()
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            # 視為台灣時區 +08:00；DB 會以 timestamptz 存
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def _to_int(s: str | None) -> Optional[int]:
    if _is_na(s): return None
    s = re.sub(r"[^\d-]", "", s.strip())
    if s == "": return None
    try: return int(s)
    except: return None

def _guess_accuracy(addr: str | None) -> int:
    a = (addr or "")
    # 超簡單規則：含「市」或「區」→ 150m，含「鄉/村」→ 800m，其餘 300m
    if ("市" in a) or ("區" in a):
        return 150
    if ("鄉" in a) or ("村" in a):
        return 800
    return 300

def ingest_csv(project_id: str, target_id: str, file_bytes: bytes) -> Dict[str, Any]:
    text = file_bytes.decode("utf-8-sig", errors="ignore")
    rdr = csv.DictReader(io.StringIO(text))
    # 允許中文欄名（範本）
    col = {k: k for k in rdr.fieldnames or []}

    total = 0
    inserted = 0
    skipped = 0
    errors: List[str] = []
    rows: List[Dict[str, Any]] = []

    for r in rdr:
        total += 1
        start_ts = _parse_ts(r.get("開始連線時間"))
        end_ts   = _parse_ts(r.get("結束連線時間")) or start_ts
        if not start_ts:
            skipped += 1
            errors.append(f"row{total}: 缺少開始連線時間")
            continue

        cell_id     = (r.get("基地台編號") or "").strip() or None
        cell_addr   = (r.get("基地台地址") or "").strip() or None
        sector_name = (r.get("細胞名稱") or "").strip() or None
        site_code   = (r.get("台號") or "").strip() or None
        sector_id   = (r.get("細胞") or "").strip() or None
        azimuth     = _to_int(r.get("方位"))

        if not cell_addr and not cell_id:
            skipped += 1
            errors.append(f"row{total}: 地址與 cell_id 皆空，無法定位")
            continue

        # 以字典查經緯度（可無，則為 None）
        ll = geocode.lookup(cell_id, cell_addr)
        lat = ll[0] if ll else None
        lng = ll[1] if ll else None
        accuracy_m = _guess_accuracy(cell_addr)

        rows.append(dict(
            project_id=project_id, target_id=target_id,
            start_ts=start_ts, end_ts=end_ts,
            cell_id=cell_id, cell_addr=cell_addr,
            sector_name=sector_name, site_code=site_code, sector_id=sector_id,
            azimuth=azimuth, lat=lat, lng=lng, accuracy_m=accuracy_m
        ))

    if rows:
        sql = """
        INSERT INTO raw_traces
          (project_id, target_id, start_ts, end_ts, cell_id, cell_addr,
           sector_name, site_code, sector_id, azimuth, lat, lng, accuracy_m, geom)
        VALUES
          (%(project_id)s, %(target_id)s, %(start_ts)s, %(end_ts)s, %(cell_id)s, %(cell_addr)s,
           %(sector_name)s, %(site_code)s, %(sector_id)s, %(azimuth)s, %(lat)s, %(lng)s, %(accuracy_m)s,
           CASE WHEN %(lat)s IS NOT NULL AND %(lng)s IS NOT NULL
                THEN ST_SetSRID(ST_MakePoint(%(lng)s, %(lat)s), 4326)
                ELSE NULL END
          )
        """
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
        inserted = len(rows)

    return {"total": total, "inserted": inserted, "skipped": skipped, "errors": errors[:50]}