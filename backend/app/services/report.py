# backend/app/services/report.py
"""
證物報告 PDF 產生器（P2）
============================================================
產出一份「法庭可呈遞」的 PDF 報告，內容：
  封面     : 案件 project_id / target_id / 產出時間 / 產出者
  證物清單 : evidence_files（filename / SHA-256 / 上傳時間 / 統計）
  軌跡摘要 : raw_traces 依 target 分組（含軟刪計數）
  Audit 時間軸：最近 N 筆 audit_logs（含 hash）

設計原則：
  - 報告本身不含個案隱私細節（不列每筆 lat/lng），只給「總量級資料」
    與「鑑識指紋」。詳細軌跡仍須以 raw_traces SELECT 取得。
  - 中文字使用 reportlab 內建 CID 字型 'STSong-Light'：免外部字型檔，
    部署到 Render（Linux）也能直出中文。
  - 報告產出本身會回頭寫一筆 audit_logs（action='export_report'）。
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
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


# ── 格式化工具 ─────────────────────────────────────────────────

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

    # ── 證物清單 ──
    story.append(Spacer(1, 10))
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

    # ── Audit 時間軸 ──
    story.append(PageBreak())
    story.append(Paragraph("三、稽核時間軸（最近 200 筆 audit_logs）", s["h1"]))
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
            "端點查詢；報告產出本身亦為一筆 audit（action='export_report'），包含本份報告請求者識別。",
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
