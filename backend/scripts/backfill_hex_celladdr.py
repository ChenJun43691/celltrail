#!/usr/bin/env python3
# backend/scripts/backfill_hex_celladdr.py
"""
Backfill：把 raw_traces 中 cell_addr 為純 hex 短碼（6–12 字）的列搬到 sector_id
─────────────────────────────────────────────────────────────────────
2026-05-24（WAKE_UP_TODO #9）

背景
====
commit a5eb683 在 ingest 層攔截 hex 短碼進 cell_addr（避免被 coverage
誤歸到 addr_geocode_failed），但「commit 之前已上傳的列」在 DB 內仍是
被污染狀態。本 script 對歷史資料做相同搬遷，順便修正既有案件 coverage
統計的分類。

策略（與 ingest fix 一致）
==========================
- 條件：`cell_addr ~ '^\\s*[0-9A-Fa-f]{6,12}\\s*$'` AND `deleted_at IS NULL`
- 可搬：sector_id 為空 → `sector_id = trim(cell_addr); cell_addr = NULL`
- 不可搬：sector_id 已被佔用 → **不動**（保留證據，需人工決定）
- 已軟刪的列一律不動（forensic 原則：歷史快照不應因事後 fix 而變動）

用法
====
    cd /Users/chenguanjun/Desktop/Python程序開發/CellTrail/backend
    source .venv/bin/activate
    python scripts/backfill_hex_celladdr.py                  # DRY RUN（預設）
    python scripts/backfill_hex_celladdr.py --apply          # 實際 UPDATE
    python scripts/backfill_hex_celladdr.py --project P-1    # 限定單一專案
    python scripts/backfill_hex_celladdr.py --apply --project P-1

輸出（DRY RUN 範例）
====================
    [DRY RUN] 共 3 個案件、共 145 列符合 hex pattern：
      P-001        :  69 可搬 |   0 sector_id 已佔用
      P-002        :  76 可搬 |   2 sector_id 已佔用
      ──────────────────────────────────────────────
      合計         : 145 可搬 |   2 sector_id 已佔用

      使用 --apply 實際執行；不加 --apply 不會動 DB。

Audit
=====
--apply 時每個受影響的 project 寫一筆 audit_logs：
    action='backfill.hex_celladdr'
    target_type='raw_traces'
    target_ref=project_id
    details={ migrated: N, sector_id_occupied: M, commit_ref: 'a5eb683' }
寫入時 user=None（script，非 user 觸發）→ user_id 為 NULL。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
from typing import Optional

HERE = pathlib.Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

# 預設環境變數（SECRET_KEY / AUTH_ENABLED 對本 script 無實質意義，
# 但 app.security 在 import 期會讀取，缺值會 fallback 到 'change-me-please'
# warning）。DATABASE_URL 必須真實 — 但延後到實際要連 DB 時才檢查，讓
# `--help` 在缺環境的機器上也能執行。
os.environ.setdefault("SECRET_KEY", "backfill-script-only")
os.environ.setdefault("AUTH_ENABLED", "true")


def _require_db_env() -> None:
    if not os.getenv("DATABASE_URL") and not os.getenv("SUPABASE_DB_URL"):
        print(
            "❌ 請先 export DATABASE_URL（或 SUPABASE_DB_URL）再執行本 script。\n"
            "   本機 docker 預設：postgresql://celltrail:celltrail@localhost:5432/celltrail",
            file=sys.stderr,
        )
        sys.exit(2)

# 條件 SQL：與 ingest fix 對齊（pattern 6–12 hex chars，允許前後空白）
_HEX_MATCH = r"^\s*[0-9A-Fa-f]{6,12}\s*$"


def _fmt_proj(p: str) -> str:
    return p if len(p) <= 12 else p[:11] + "…"


def survey(project_filter: Optional[str]) -> dict:
    """
    回傳：
      {
        "per_project": [
          {"project_id": str, "migratable": int, "sector_occupied": int},
          ...
        ],
        "totals": {"migratable": int, "sector_occupied": int},
      }
    """
    from app.db.session import get_conn

    where = ["cell_addr ~ %s", "deleted_at IS NULL"]
    params: list = [_HEX_MATCH]
    if project_filter:
        where.append("project_id = %s")
        params.append(project_filter)
    where_sql = " AND ".join(where)

    sql = f"""
    SELECT project_id,
           COUNT(*) FILTER (WHERE sector_id IS NULL OR sector_id = '')   AS migratable,
           COUNT(*) FILTER (WHERE sector_id IS NOT NULL AND sector_id <> '') AS sector_occupied
      FROM raw_traces
     WHERE {where_sql}
     GROUP BY project_id
     ORDER BY project_id
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params, prepare=False)
        rows = cur.fetchall()

    per_project = [
        {"project_id": r[0], "migratable": int(r[1]), "sector_occupied": int(r[2])}
        for r in rows
    ]
    totals = {
        "migratable": sum(p["migratable"] for p in per_project),
        "sector_occupied": sum(p["sector_occupied"] for p in per_project),
    }
    return {"per_project": per_project, "totals": totals}


def apply_backfill(project_filter: Optional[str]) -> dict:
    """
    實際 UPDATE。回傳 {"per_project": [...with migrated count]}，
    每個 project 的 migrated 數會用於 audit_logs。
    """
    from app.db.session import get_conn
    from app.services.audit import write_audit

    # 先取「將要搬」的 project 清單與筆數（用於後續 audit；放在 UPDATE
    # 之前以避免「UPDATE 後 SELECT 已對不到該列」）
    where = ["cell_addr ~ %s", "deleted_at IS NULL",
             "(sector_id IS NULL OR sector_id = '')"]
    params: list = [_HEX_MATCH]
    if project_filter:
        where.append("project_id = %s")
        params.append(project_filter)
    where_sql = " AND ".join(where)

    select_sql = f"""
    SELECT project_id, COUNT(*) AS n
      FROM raw_traces
     WHERE {where_sql}
     GROUP BY project_id
    """
    update_sql = f"""
    UPDATE raw_traces
       SET sector_id = trim(cell_addr),
           cell_addr = NULL
     WHERE {where_sql}
    """

    per_project: list[dict] = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(select_sql, params, prepare=False)
        for pid, n in cur.fetchall():
            per_project.append({"project_id": pid, "migrated": int(n)})

        cur.execute(update_sql, params, prepare=False)
        total_updated = cur.rowcount

    # sanity check：UPDATE rowcount 應等於 per_project 加總
    expected = sum(p["migrated"] for p in per_project)
    if total_updated != expected:
        print(
            f"⚠ UPDATE rowcount={total_updated} 與 SELECT 加總={expected} 不一致；"
            f"可能有並發寫入。仍會以 SELECT 結果寫 audit。",
            file=sys.stderr,
        )

    # 每個受影響的 project 寫一筆 audit（user=None → user_id 為 NULL）
    for p in per_project:
        write_audit(
            action="backfill.hex_celladdr",
            user=None,
            target_type="raw_traces",
            target_ref=p["project_id"],
            project_id=p["project_id"],
            details={
                "migrated": p["migrated"],
                "commit_ref": "a5eb683",
                "script": "backfill_hex_celladdr.py",
                "pattern": _HEX_MATCH,
            },
            status_code=200,
        )

    return {"per_project": per_project, "total_updated": total_updated}


def print_survey(result: dict, applied: bool) -> None:
    label = "[APPLIED]" if applied else "[DRY RUN]"
    pp = result["per_project"]
    if not pp:
        print(f"{label} 無符合條件的列。已乾淨，不需要搬。")
        return

    if applied:
        total = sum(p["migrated"] for p in pp)
        print(f"\n{label} 已搬 {len(pp)} 個案件、共 {total} 列：\n")
        for p in pp:
            print(f"  {_fmt_proj(p['project_id']):<14}: {p['migrated']:>4} 列已搬")
        print(f"  {'──────────────────────────────':<14}")
        print(f"  {'合計':<14}: {total:>4} 列")
        return

    totals = result["totals"]
    print(
        f"\n{label} 共 {len(pp)} 個案件、共 "
        f"{totals['migratable'] + totals['sector_occupied']} 列符合 hex pattern：\n"
    )
    for p in pp:
        print(
            f"  {_fmt_proj(p['project_id']):<14}: "
            f"{p['migratable']:>4} 可搬 | "
            f"{p['sector_occupied']:>4} sector_id 已佔用"
        )
    print(f"  {'──────────────────────────────':<14}")
    print(
        f"  {'合計':<14}: "
        f"{totals['migratable']:>4} 可搬 | "
        f"{totals['sector_occupied']:>4} sector_id 已佔用"
    )
    if totals["sector_occupied"]:
        print(
            "\n  ⚠ sector_id 已佔用的列不會被搬（避免覆蓋既有 sector 證據）。\n"
            "    這些列的 cell_addr 仍為 hex，需人工判斷如何處理。"
        )
    print("\n  使用 --apply 實際執行；不加 --apply 不會動 DB。")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--apply", action="store_true",
                    help="實際 UPDATE + 寫 audit；不加只做 DRY RUN")
    ap.add_argument("--project", default=None,
                    help="限定單一 project_id（不指定 = 全 DB）")
    args = ap.parse_args()

    # --help 已 return；以下都會碰 DB
    _require_db_env()

    if not args.apply:
        result = survey(args.project)
        print_survey(result, applied=False)
        return 0

    # --apply 路徑：先 survey 顯示 → 等 2 秒讓使用者 Ctrl-C 反悔 → 才執行
    pre = survey(args.project)
    print_survey(pre, applied=False)
    if pre["totals"]["migratable"] == 0:
        print("\n沒有可搬的列，--apply 也不需要執行。")
        return 0

    print(f"\n  即將實際執行 UPDATE…（Ctrl-C 中止）")
    import time
    for s in range(3, 0, -1):
        print(f"    {s}…", end="\r", flush=True)
        time.sleep(1)

    result = apply_backfill(args.project)
    print_survey(result, applied=True)
    return 0


def _close_pool() -> None:
    """避免退出時 ConnectionPool 的 worker thread 來不及 join 的 warning。
    app.db.session 的 pool 是 module-level singleton；script 結束前主動 close
    才能讓退出乾淨（不影響服務本身，因為服務不會 import 本 script）。"""
    try:
        from app.db.session import pool
        if not pool.closed:
            pool.close()
    except Exception:
        pass


if __name__ == "__main__":
    rc = main()
    _close_pool()
    sys.exit(rc)
