# backend/app/services/report.py
"""
證物報告 PDF 產生器（P2 + 地圖截圖）
============================================================
產出一份「法庭可呈遞」的 PDF 報告，內容：
  封面     : 案件 project_id / target_id / 產出時間 / 產出者
  地圖快覽 : OSM 靜態底圖 + 所有定位點（staticmap.py 合成）
  證物清單 : evidence_files（filename / SHA-256 / 上傳時間 / 統計）
  軌跡摘要 : raw_traces 依 target 分組（含軟刪計數）
  方位角   : azimuth_ref 標註狀態（法庭可防禦性 P2.5-C）
  Audit 時間軸：最近 N 筆 audit_logs（含 hash）

設計原則：
  - 地圖為 OSM 靜態圖磚拼接（無瀏覽器依賴）；fetch 失敗時退化為灰底仍繪點位。
  - 報告本身不含每筆 lat/lng，只給「總量級資料」與「鑑識指紋」。
  - 中文字使用 reportlab 內建 CID 字型 'STSong-Light'：免外部字型檔。
  - 報告產出本身會回頭寫一筆 audit_logs（action='export_report'）。
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    Image as RLImage,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.db.session import get_conn

# ── 中文字型：reportlab 內建 CID（不依賴系統字型檔）──────────────
# STSong-Light 是 Adobe 提供的內建字型 placeholder，多數 PDF reader 會
# 自動套用系統 Source Han / Noto CJK；萬一有方塊，再考慮註冊自訂 ttf。
_CN_FONT = "STSong-Light"
try:
    pdfmetrics.registerFont(UnicodeCIDFont(_CN_FONT))
except Exception as e:
    # 萬一連 CID 都裝不起來（極罕見），fallback 到 Helvetica（中文會方塊但不會崩潰）
    print(f"[report] WARN 載入 {_CN_FONT} 失敗：{e}；使用 Helvetica fallback")
    _CN_FONT = "Helvetica"


def _styles() -> Dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title":    ParagraphStyle("title",   parent=base["Title"],     fontName=_CN_FONT, fontSize=20, leading=26, spaceAfter=8),
        "h1":       ParagraphStyle("h1",      parent=base["Heading1"],  fontName=_CN_FONT, fontSize=14, leading=18, spaceBefore=10, spaceAfter=6),
        "h2":       ParagraphStyle("h2",      parent=base["Heading2"],  fontName=_CN_FONT, fontSize=12, leading=16, spaceBefore=6,  spaceAfter=4),
        "body":     ParagraphStyle("body",    parent=base["BodyText"],  fontName=_CN_FONT, fontSize=10, leading=14, alignment=TA_LEFT),
        "small":    ParagraphStyle("small",   parent=base["BodyText"],  fontName=_CN_FONT, fontSize=8,  leading=11, textColor=colors.grey),
        "code":     ParagraphStyle("code",    parent=base["BodyText"],  fontName="Courier",fontSize=8,  leading=10, textColor=colors.HexColor("#444")),
    }


# ── 資料抓取 ────────────────────────────────────────────────────

def _fetch_evidence(project_id: str, target_id: Optional[str]) -> List[Dict[str, Any]]:
    where = ["project_id = %s"]
    params: List[Any] = [project_id]
    if target_id:
        where.append("target_id = %s"); params.append(target_id)
    sql = f"""
    SELECT id, target_id, filename, ext, size_bytes, sha256_full,
           uploaded_by_name, uploaded_at,
           rows_total, rows_inserted, rows_skipped
      FROM evidence_files
     WHERE {' AND '.join(where)}
     ORDER BY uploaded_at DESC, id DESC
    """
    items = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params, prepare=False)
        for r in cur.fetchall():
            items.append({
                "id": r[0], "target_id": r[1], "filename": r[2], "ext": r[3],
                "size_bytes": r[4], "sha256_full": r[5],
                "uploaded_by_name": r[6], "uploaded_at": r[7],
                "rows_total": r[8], "rows_inserted": r[9], "rows_skipped": r[10],
            })
    return items


def _fetch_azimuth_summary(project_id: str, target_id: Optional[str]) -> List[Dict[str, Any]]:
    """
    方位角北方基準標註狀態（P2.5-C）：
    每 target 的 azimuth_ref 分佈 + 最後標註人/時間/書面依據。
    """
    where_rt = ["project_id = %s", "deleted_at IS NULL"]
    params_rt: List[Any] = [project_id]
    if target_id:
        where_rt.append("target_id = %s"); params_rt.append(target_id)
    ref_sql = f"""
    SELECT target_id, azimuth_ref, COUNT(*) AS cnt
      FROM raw_traces
     WHERE {' AND '.join(where_rt)}
     GROUP BY target_id, azimuth_ref
     ORDER BY target_id, azimuth_ref
    """

    where_al = ["project_id = %s", "action = 'update_azimuth_ref'"]
    params_al: List[Any] = [project_id]
    if target_id:
        where_al.append("target_ref = %s"); params_al.append(target_id)
    ann_sql = f"""
    SELECT DISTINCT ON (target_ref)
           target_ref,
           username,
           ts,
           details->>'evidence' AS evidence,
           details->>'ref'      AS ref
      FROM audit_logs
     WHERE {' AND '.join(where_al)}
     ORDER BY target_ref, ts DESC
    """

    targets: dict = {}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(ref_sql, params_rt, prepare=False)
        for tid, ref, cnt in cur.fetchall():
            if tid not in targets:
                targets[tid] = {"target_id": tid, "by_ref": {}, "total": 0,
                                "last_annotator": None, "last_annotated_at": None,
                                "last_evidence": None, "last_ref": None}
            targets[tid]["by_ref"][ref] = int(cnt)
            targets[tid]["total"] += int(cnt)

        cur.execute(ann_sql, params_al, prepare=False)
        for tid, username, ts, evidence, ref in cur.fetchall():
            if tid in targets:
                targets[tid]["last_annotator"]    = username
                targets[tid]["last_annotated_at"] = ts
                targets[tid]["last_evidence"]     = evidence
                targets[tid]["last_ref"]          = ref

    items = []
    for t in sorted(targets.values(), key=lambda x: x["target_id"]):
        total   = t["total"]
        unknown = t["by_ref"].get("unknown", 0)
        t["unknown_pct"] = round(unknown / total * 100, 1) if total else 0.0
        items.append(t)
    return items


def _fetch_trace_summary(project_id: str, target_id: Optional[str]) -> List[Dict[str, Any]]:
    """以 target 分組統計：總筆數、已定位、未定位、軟刪、最早/最晚"""
    where = ["project_id = %s"]
    params: List[Any] = [project_id]
    if target_id:
        where.append("target_id = %s"); params.append(target_id)
    sql = f"""
    SELECT target_id,
           COUNT(*)                                    AS total,
           COUNT(*) FILTER (WHERE deleted_at IS NULL)  AS active,
           COUNT(*) FILTER (WHERE deleted_at IS NOT NULL) AS soft_deleted,
           COUNT(*) FILTER (WHERE deleted_at IS NULL AND geom IS NOT NULL) AS located,
           COUNT(*) FILTER (WHERE deleted_at IS NULL AND geom IS NULL)     AS unlocated,
           MIN(start_ts) FILTER (WHERE deleted_at IS NULL)                 AS earliest_ts,
           MAX(start_ts) FILTER (WHERE deleted_at IS NULL)                 AS latest_ts
      FROM raw_traces
     WHERE {' AND '.join(where)}
     GROUP BY target_id
     ORDER BY target_id
    """
    items = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params, prepare=False)
        for r in cur.fetchall():
            items.append({
                "target_id": r[0], "total": r[1], "active": r[2], "soft_deleted": r[3],
                "located": r[4], "unlocated": r[5],
                "earliest_ts": r[6], "latest_ts": r[7],
            })
    return items


def _fetch_audit(project_id: str, target_id: Optional[str], limit: int = 200) -> List[Dict[str, Any]]:
    where = ["project_id = %s"]
    params: List[Any] = [project_id]
    if target_id:
        where.append("target_ref = %s"); params.append(target_id)
    sql = f"""
    SELECT ts, action, username, role, ip, status_code, payload_hash, error_text
      FROM audit_logs
     WHERE {' AND '.join(where)}
     ORDER BY ts DESC, id DESC
     LIMIT %s
    """
    items = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, [*params, limit], prepare=False)
        for r in cur.fetchall():
            items.append({
                "ts": r[0], "action": r[1], "username": r[2], "role": r[3],
                "ip": r[4], "status_code": r[5], "payload_hash": r[6], "error_text": r[7],
            })
    return items


def _fetch_map_points(project_id: str, target_id: Optional[str], limit: int = 2000) -> List[Dict[str, Any]]:
    """
    抓取所有已定位點位的 lat/lng + target_id，供靜態地圖產生器使用。
    PostGIS geom 儲存格式為 EPSG:4326，直接用 ST_Y/ST_X 取出。
    limit=2000：報告地圖是視覺縮圖，無需全部點位；超過 2000 點在地圖上也難以分辨。
    """
    where = ["project_id = %s", "deleted_at IS NULL", "geom IS NOT NULL"]
    params: List[Any] = [project_id]
    if target_id:
        where.append("target_id = %s"); params.append(target_id)
    sql = f"""
    SELECT ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lng,
           target_id, azimuth_ref, accuracy_m
      FROM raw_traces
     WHERE {' AND '.join(where)}
     ORDER BY target_id, start_ts
     LIMIT %s
    """
    items = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, [*params, limit], prepare=False)
        for r in cur.fetchall():
            items.append({
                "lat": float(r[0]), "lng": float(r[1]),
                "target_id": r[2], "azimuth_ref": r[3],
                "accuracy_m": float(r[4]) if r[4] is not None else None,
            })
    return items


# ── 格式化工具 ─────────────────────────────────────────────────

def _fit_image_dims(w_px: int, h_px: int, max_w: float, max_h: float):
    """
    等比縮放圖片（像素 w_px×h_px）至「寬不超過 max_w 且高不超過 max_h」，
    回傳 reportlab 用的 (width, height)（單位 pt）。

    為什麼要同時鎖寬與高（2026-05-30 修）：
      地圖截圖的 aspect ratio 由 bbox 決定，可能是高瘦的直式圖。若只鎖寬度、
      高度任由 aspect ratio 放大，會超出 A4 頁框可用高度，reportlab 在
      Flowable 排版時拋 LayoutError，導致整份證物報告產不出來（500）。
    """
    map_w = max_w
    map_h = max_w * h_px / w_px
    if map_h > max_h:
        map_h = max_h
        map_w = max_h * w_px / h_px
    return map_w, map_h


def _fmt_ts(ts: Optional[datetime]) -> str:
    if not ts: return ""
    try:
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def _fmt_size(n: Optional[int]) -> str:
    if n is None: return ""
    if n < 1024: return f"{n} B"
    if n < 1024*1024: return f"{n/1024:.1f} KB"
    return f"{n/1024/1024:.2f} MB"


def _short_hash(h: Optional[str], n: int = 16) -> str:
    if not h: return ""
    return h[:n] + ("…" if len(h) > n else "")


# ── 主流程 ─────────────────────────────────────────────────────

def build_evidence_report(
    *,
    project_id: str,
    target_id: Optional[str],
    requested_by: Optional[str] = None,
) -> bytes:
    """
    產生 PDF bytes。呼叫者再決定如何回應（StreamingResponse / 存檔）。
    """
    s = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm, topMargin=18*mm, bottomMargin=18*mm,
        title=f"CellTrail 證物報告 {project_id}",
        author=requested_by or "CellTrail",
    )

    story: List[Any] = []

    # ── 封面 ──
    story.append(Paragraph("CellTrail 證物報告", s["title"]))
    story.append(Paragraph("Cell Trail Evidence Report", s["small"]))
    story.append(Spacer(1, 6))

    cover_data = [
        ["專案 (Project ID)", project_id],
        ["目標 (Target ID)",   target_id or "（全部 target）"],
        ["產出時間",           _fmt_ts(datetime.now(timezone.utc))],
        ["產出者",             requested_by or "（未識別）"],
        ["報告版本",           "v1（含 audit 時間軸 / 全 SHA-256 證物指紋）"],
    ]
    t = Table(cover_data, colWidths=[40*mm, 130*mm])
    t.setStyle(TableStyle([
        ("FONTNAME",  (0,0), (-1,-1), _CN_FONT),
        ("FONTSIZE",  (0,0), (-1,-1), 10),
        ("BACKGROUND",(0,0), (0,-1),  colors.HexColor("#f0f3f9")),
        ("LINEBELOW", (0,0), (-1,-1), 0.3, colors.HexColor("#cdd3df")),
        ("VALIGN",    (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING",  (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING",   (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0), (-1,-1), 4),
    ]))
    story.append(t)

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "本報告由 CellTrail 系統自動產生。所附 SHA-256 為原始檔案於進入系統時的"
        "byte-for-byte 雜湊指紋；所有上傳、刪除、還原行為皆於 audit_logs 留有"
        "append-only 紀錄並可驗證。",
        s["small"],
    ))

    # ── 地圖快覽（OSM 靜態圖磚合成）──
    story.append(PageBreak())
    story.append(Paragraph("地圖快覽（基地台連線點位）", s["h1"]))
    story.append(Paragraph(
        "以下地圖由系統於報告產出時即時從 OpenStreetMap 圖磚伺服器合成，"
        "各顏色對應不同 target；點位為 raw_traces 已定位記錄（geom IS NOT NULL）。"
        "底圖版權：© OpenStreetMap contributors（CC-BY-SA）。",
        s["small"],
    ))
    story.append(Spacer(1, 4))
    map_points = _fetch_map_points(project_id, target_id)
    if map_points:
        try:
            from app.services.staticmap import build_map_image, color_legend
            png_bytes = build_map_image(map_points, output_w=760, max_side=4)
            if png_bytes:
                # 等比縮放，但同時受限於頁框「寬」與「高」（見 _fit_image_dims）。
                # max_w/max_h 取略小於 A4 頁框可用區（約 481×728pt），並為圖說
                # 與圖例保留同頁空間。
                pil = PILImage.open(io.BytesIO(png_bytes))
                w_px, h_px = pil.size
                map_w, map_h = _fit_image_dims(w_px, h_px, 168 * mm, 200 * mm)
                story.append(RLImage(io.BytesIO(png_bytes), width=map_w, height=map_h))
                story.append(Spacer(1, 4))
                # 圖例 Table（避免中文在 PIL 預設字型顯示不佳，改用 reportlab 繪製）
                legend = color_legend(list(dict.fromkeys(p["target_id"] for p in map_points)))
                if legend:
                    leg_rows = []
                    for tid, hex_color in legend:
                        r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
                        leg_rows.append([" ", tid])
                    leg_tbl = Table(leg_rows, colWidths=[8*mm, 80*mm])
                    leg_cmds = [
                        ("FONTNAME",      (0,0), (-1,-1), _CN_FONT),
                        ("FONTSIZE",      (0,0), (-1,-1), 9),
                        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
                        ("TOPPADDING",    (0,0), (-1,-1), 2),
                        ("BOTTOMPADDING", (0,0), (-1,-1), 2),
                    ]
                    for i, (_, hex_color) in enumerate(legend):
                        r2 = int(hex_color[1:3], 16) / 255
                        g2 = int(hex_color[3:5], 16) / 255
                        b2 = int(hex_color[5:7], 16) / 255
                        leg_cmds.append(("BACKGROUND", (0,i), (0,i), colors.Color(r2, g2, b2, 0.85)))
                    leg_tbl.setStyle(TableStyle(leg_cmds))
                    story.append(leg_tbl)
                story.append(Paragraph(
                    f"共 {len(map_points)} 個定位點位（最多顯示 2000 筆）。",
                    s["small"],
                ))
            else:
                story.append(Paragraph("（地圖合成失敗，請確認網路或查看 uvicorn log）", s["body"]))
        except Exception as _map_exc:
            story.append(Paragraph(f"（地圖產生發生例外：{type(_map_exc).__name__}: {_map_exc}）", s["body"]))
    else:
        story.append(Paragraph("（無已定位點位，地圖省略）", s["body"]))

    # ── 證物清單 ──
    story.append(PageBreak())
    story.append(Paragraph("一、證物檔案清單（evidence_files）", s["h1"]))
    evidences = _fetch_evidence(project_id, target_id)
    if not evidences:
        story.append(Paragraph("（無證物紀錄）", s["body"]))
    else:
        rows = [["#", "檔名 / 副檔名", "Target", "大小", "SHA-256（前 16 字元）", "列數（總/入庫/略過）", "上傳者 / 時間"]]
        for i, e in enumerate(evidences, 1):
            rows.append([
                str(i),
                f"{e['filename']}\n.{e['ext'] or ''}",
                e["target_id"] or "",
                _fmt_size(e["size_bytes"]),
                _short_hash(e["sha256_full"], 16),
                f"{e['rows_total'] or '-'} / {e['rows_inserted'] or '-'} / {e['rows_skipped'] or '-'}",
                f"{e['uploaded_by_name'] or '-'}\n{_fmt_ts(e['uploaded_at'])}",
            ])
        tbl = Table(rows, colWidths=[8*mm, 40*mm, 22*mm, 18*mm, 35*mm, 25*mm, 32*mm], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("FONTNAME", (0,0), (-1,-1), _CN_FONT),
            ("FONTSIZE", (0,0), (-1,-1), 8),
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#0b5ed7")),
            ("TEXTCOLOR",(0,0),(-1,0), colors.white),
            ("LINEBELOW",(0,0),(-1,-1), 0.25, colors.HexColor("#cdd3df")),
            ("VALIGN",   (0,0),(-1,-1), "TOP"),
            ("LEFTPADDING",  (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
            ("TOPPADDING",   (0,0), (-1,-1), 3),
            ("BOTTOMPADDING",(0,0), (-1,-1), 3),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f"共 {len(evidences)} 筆證物。完整 SHA-256（64 字元）請見 <code>/api/projects/{{project}}/evidence-files</code> 端點。",
            s["small"],
        ))

    # ── 軌跡摘要 ──
    story.append(PageBreak())
    story.append(Paragraph("二、軌跡資料摘要（raw_traces 依 target 分組）", s["h1"]))
    summary = _fetch_trace_summary(project_id, target_id)
    if not summary:
        story.append(Paragraph("（無 raw_traces 紀錄）", s["body"]))
    else:
        rows = [["Target", "總筆數", "在線", "軟刪", "已定位", "未定位", "最早", "最晚"]]
        for it in summary:
            rows.append([
                it["target_id"],
                str(it["total"]), str(it["active"]), str(it["soft_deleted"]),
                str(it["located"]), str(it["unlocated"]),
                _fmt_ts(it["earliest_ts"]), _fmt_ts(it["latest_ts"]),
            ])
        tbl = Table(rows, colWidths=[26*mm, 16*mm, 14*mm, 14*mm, 16*mm, 16*mm, 32*mm, 32*mm], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("FONTNAME", (0,0), (-1,-1), _CN_FONT),
            ("FONTSIZE", (0,0), (-1,-1), 9),
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#0b5ed7")),
            ("TEXTCOLOR",(0,0),(-1,0), colors.white),
            ("ALIGN",    (1,1),(5,-1), "RIGHT"),
            ("LINEBELOW",(0,0),(-1,-1), 0.25, colors.HexColor("#cdd3df")),
            ("VALIGN",   (0,0),(-1,-1), "MIDDLE"),
            ("LEFTPADDING",  (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
            ("TOPPADDING",   (0,0), (-1,-1), 3),
            ("BOTTOMPADDING",(0,0), (-1,-1), 3),
        ]))
        story.append(tbl)

    # ── 方位角北方基準標註狀態（P2.5-C）──
    story.append(PageBreak())
    story.append(Paragraph("三、方位角北方基準標註狀態（P2.5-C 法庭可防禦性）", s["h1"]))
    story.append(Paragraph(
        "說明：電信業者 azimuth（方位角）的「北方基準」無統一規格（磁北 vs 真北）。"
        "台灣磁偏角約 -4°~-5°，500m 距離下差異可達 50m。"
        "法庭質疑「此方位角基準為何」時，本表提供書面依據與標註稽核鏈。",
        s["small"],
    ))
    story.append(Spacer(1, 6))
    az_summary = _fetch_azimuth_summary(project_id, target_id)
    if not az_summary:
        story.append(Paragraph("（無含 azimuth 欄位的 target）", s["body"]))
    else:
        rows = [["Target", "總筆數", "unknown", "magnetic", "true", "unknown%", "最後標註者", "標註時間", "書面依據（摘錄）"]]
        for t in az_summary:
            by_ref = t["by_ref"]
            rows.append([
                t["target_id"],
                str(t["total"]),
                str(by_ref.get("unknown", 0)),
                str(by_ref.get("magnetic", 0)),
                str(by_ref.get("true", 0)),
                f"{t['unknown_pct']}%",
                t["last_annotator"] or "（未標註）",
                _fmt_ts(t["last_annotated_at"]) if t["last_annotated_at"] else "—",
                (t["last_evidence"] or "—")[:40],
            ])

        col_w = [26*mm, 14*mm, 16*mm, 16*mm, 12*mm, 16*mm, 20*mm, 28*mm, 42*mm]
        tbl = Table(rows, colWidths=col_w, repeatRows=1)

        # 高亮 unknown%：若 >0 用橙底，=0 用綠底
        style_cmds = [
            ("FONTNAME",      (0,0), (-1,-1), _CN_FONT),
            ("FONTSIZE",      (0,0), (-1,-1), 8),
            ("BACKGROUND",    (0,0), (-1,0),  colors.HexColor("#0b5ed7")),
            ("TEXTCOLOR",     (0,0), (-1,0),  colors.white),
            ("LINEBELOW",     (0,0), (-1,-1), 0.25, colors.HexColor("#cdd3df")),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
            ("LEFTPADDING",   (0,0), (-1,-1), 4),
            ("RIGHTPADDING",  (0,0), (-1,-1), 4),
            ("TOPPADDING",    (0,0), (-1,-1), 3),
            ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ]
        for row_idx, t in enumerate(az_summary, start=1):
            if t["unknown_pct"] == 0:
                style_cmds.append(("BACKGROUND", (5, row_idx), (5, row_idx), colors.HexColor("#e8f5e9")))
                style_cmds.append(("TEXTCOLOR",  (5, row_idx), (5, row_idx), colors.HexColor("#1f7a3f")))
            else:
                style_cmds.append(("BACKGROUND", (5, row_idx), (5, row_idx), colors.HexColor("#ffebee")))
                style_cmds.append(("TEXTCOLOR",  (5, row_idx), (5, row_idx), colors.HexColor("#b3261e")))

        tbl.setStyle(TableStyle(style_cmds))
        story.append(tbl)
        story.append(Spacer(1, 4))
        all_unknown = sum(t["by_ref"].get("unknown", 0) for t in az_summary)
        all_total   = sum(t["total"] for t in az_summary)
        pct = round(all_unknown / all_total * 100, 1) if all_total else 0.0
        story.append(Paragraph(
            f"全案 unknown 比例：{pct}%（{all_unknown} / {all_total} 筆尚未確認北方基準）。"
            "unknown > 0% 者在法庭上無法回答「方位角北方基準為何」，建議於出庭前完成標註。",
            s["small"],
        ))

    # ── Audit 時間軸 ──
    story.append(PageBreak())
    story.append(Paragraph("四、稽核時間軸（最近 200 筆 audit_logs）", s["h1"]))
    audit = _fetch_audit(project_id, target_id, limit=200)
    if not audit:
        story.append(Paragraph("（無 audit_logs 紀錄）", s["body"]))
    else:
        rows = [["時間", "Action", "User / Role", "IP", "Status", "Hash（前 12）"]]
        for a in audit:
            rows.append([
                _fmt_ts(a["ts"]),
                a["action"] or "",
                f"{a['username'] or '-'} / {a['role'] or '-'}",
                a["ip"] or "",
                str(a["status_code"]) if a["status_code"] is not None else "-",
                _short_hash(a["payload_hash"], 12),
            ])
        tbl = Table(rows, colWidths=[36*mm, 32*mm, 30*mm, 26*mm, 14*mm, 32*mm], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("FONTNAME", (0,0), (-1,-1), _CN_FONT),
            ("FONTSIZE", (0,0), (-1,-1), 8),
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#0b5ed7")),
            ("TEXTCOLOR",(0,0),(-1,0), colors.white),
            ("LINEBELOW",(0,0),(-1,-1), 0.25, colors.HexColor("#cdd3df")),
            ("VALIGN",   (0,0),(-1,-1), "MIDDLE"),
            ("LEFTPADDING",  (0,0), (-1,-1), 4),
            ("RIGHTPADDING", (0,0), (-1,-1), 4),
            ("TOPPADDING",   (0,0), (-1,-1), 2),
            ("BOTTOMPADDING",(0,0), (-1,-1), 2),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            "如需完整 audit 紀錄與 details JSON，請以 <code>/api/audit/logs?project_id=...</code> "
            "端點查詢；報告產出本身亦為一筆 audit（action='export_report'），包含本份報告請求者識別。"
            "方位角標註稽核請過濾 action='update_azimuth_ref'。",
            s["small"],
        ))

    # ── 頁尾說明 ──
    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "—— 報告結束 —— "
        "本報告為 CellTrail 系統自動產出，內容真實性以 audit_logs / evidence_files "
        "資料庫表為準。如有疑義請以資料庫端 SQL 對照驗證。",
        s["small"],
    ))

    doc.build(story)
    return buf.getvalue()
