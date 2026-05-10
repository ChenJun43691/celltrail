# app/api/targets.py
"""
Target 管理端點：
  - DELETE /projects/{project_id}/targets/{target_id}            軟刪整個 target
  - POST   /projects/{project_id}/targets/{target_id}/restore    還原（管理員）
  - GET    /projects/{project_id}/targets/{target_id}/deleted    列出已軟刪的 raw_traces

P1 改動（2026-04-26）：
  原本 DELETE 會直接 `DELETE FROM raw_traces`，視同事證滅失。
  改為 UPDATE deleted_at = now()，搭配 audit_logs 記錄誰刪、何時刪、為何刪。
  證物保全的核心：「刪不掉的紀錄」 + 「可還原的軟刪」。
"""
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.db.session import get_conn
from app.security import AUTH_ENABLED, assert_project_access, get_current_user, require_admin
from app.services.audit import write_audit
from app.services.ingest import _insert_records, _parse_ts

router = APIRouter()


class DeleteTargetIn(BaseModel):
    """軟刪請求 body：強制要求填刪除理由（法庭可防禦性要件）"""
    reason: str


@router.delete(
    "/projects/{project_id}/targets/{target_id}",
    dependencies=[Depends(get_current_user)],
)
def delete_target(
    project_id: str,
    target_id: str,
    request: Request,
    body: Optional[DeleteTargetIn] = Body(None),
    current_user: dict = Depends(get_current_user),
):
    """
    軟刪一個 target 底下的所有 raw_traces。

    可由 body 傳 { "reason": "..." }；若未提供，audit log 仍會記但 reason 留空。
    舊版 cURL 沒帶 body 也能繼續用，避免相容性中斷。
    """
    assert_project_access(current_user, project_id, "owner")
    reason = (body.reason if body else None) or None
    user_id = current_user.get("id")

    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE raw_traces
                   SET deleted_at = now(),
                       deleted_by = %s,
                       delete_reason = %s
                 WHERE project_id = %s
                   AND target_id  = %s
                   AND deleted_at IS NULL
                """,
                (user_id, reason, project_id, target_id),
                prepare=False,
            )
            deleted = cur.rowcount
    except Exception as e:
        write_audit(
            action="delete_target_failed",
            user=current_user, request=request,
            target_type="target", target_ref=target_id, project_id=project_id,
            details={"reason": reason},
            status_code=500, error_text=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(status_code=500, detail=f"軟刪失敗：{type(e).__name__}: {e}")

    if deleted == 0:
        # 不寫 audit（避免被攻擊者拿來做存在性偵測）
        raise HTTPException(status_code=404, detail="Target 不存在或已軟刪")

    write_audit(
        action="delete_target",
        user=current_user, request=request,
        target_type="target", target_ref=target_id, project_id=project_id,
        details={"affected_rows": deleted, "reason": reason},
        status_code=200,
    )
    return {
        "ok": True,
        "soft_deleted": deleted,
        "project_id": project_id,
        "target_id": target_id,
        "note": "軟刪：紀錄保留於 raw_traces，僅 deleted_at 標記為非 NULL。可由 admin 經 /restore 端點還原。",
    }


class RestoreTargetIn(BaseModel):
    reason: str


@router.post(
    "/projects/{project_id}/targets/{target_id}/restore",
    dependencies=[Depends(require_admin)],   # 還原須 admin（避免一般員工把刪除誤操作蓋掉）
)
def restore_target(
    project_id: str,
    target_id: str,
    request: Request,
    body: RestoreTargetIn,
    current_user: dict = Depends(get_current_user),
):
    """還原軟刪的 target 紀錄；強制填還原理由。需 owner 以上。"""
    assert_project_access(current_user, project_id, "owner")
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE raw_traces
                   SET deleted_at = NULL,
                       deleted_by = NULL,
                       delete_reason = NULL
                 WHERE project_id = %s
                   AND target_id  = %s
                   AND deleted_at IS NOT NULL
                """,
                (project_id, target_id),
                prepare=False,
            )
            restored = cur.rowcount
    except Exception as e:
        write_audit(
            action="restore_target_failed",
            user=current_user, request=request,
            target_type="target", target_ref=target_id, project_id=project_id,
            details={"reason": body.reason},
            status_code=500, error_text=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(status_code=500, detail=f"還原失敗：{type(e).__name__}: {e}")

    if restored == 0:
        raise HTTPException(status_code=404, detail="找不到已軟刪的紀錄可還原")

    write_audit(
        action="restore_target",
        user=current_user, request=request,
        target_type="target", target_ref=target_id, project_id=project_id,
        details={"restored_rows": restored, "reason": body.reason},
        status_code=200,
    )
    return {"ok": True, "restored": restored, "project_id": project_id, "target_id": target_id}


# ============================================================
# 方位角基準標註（P2.5 法庭可防禦性）
# ============================================================
class UpdateAzimuthRefIn(BaseModel):
    """
    更新方位角基準的請求 body。

    為什麼 ref 用 Literal 而不是 str：
      Pydantic Literal 會在進入端點前做白名單驗證，省去手寫 if-else，
      且 OpenAPI 文件直接顯示三個合法值，呼叫端一目了然。

    為什麼 evidence 必填且要求至少 5 字：
      此操作直接影響法庭對 azimuth 欄位的採信。標註為 'magnetic' 或 'true'
      時必須提供書面依據（電信業者函覆、規格書頁碼等），否則沒辦法回追
      「為什麼當初認定是磁北/真北」。即使是 'unknown' 也要寫 evidence
      （例：「電信業者拒絕回覆，依保守原則維持 unknown」）。
    """
    ref: Literal["magnetic", "true", "unknown"]
    evidence: str = Field(..., min_length=5,
                          description="書面依據描述（例：中華電信 2026-01-15 函覆 XXX 號）")


@router.patch(
    "/projects/{project_id}/targets/{target_id}/azimuth-ref",
    dependencies=[Depends(require_admin)],
)
def update_azimuth_ref(
    project_id: str,
    target_id: str,
    request: Request,
    body: UpdateAzimuthRefIn,
    current_user: dict = Depends(get_current_user),
):
    """
    批次更新某 target 全部 raw_traces 的 azimuth_ref。

    法庭意義：
      電信業者交付的 azimuth 北方基準（磁北/真北）並無統一規格；
      偵查員須查證書面交付規格後，透過此端點批次標註。
      此操作會在 audit_logs 留下完整紀錄，包括：
        - 誰標註（current_user.id）
        - 何時標註（audit_logs.ts）
        - 標註成什麼值（details.ref）
        - 依據何書面證據（details.evidence）
        - 影響幾筆紀錄（details.affected_rows）
      法庭日後可逐案還原此標註的合理性。

    僅針對「未軟刪」的紀錄（deleted_at IS NULL）；
    若需更新已軟刪的紀錄（罕見），請先 restore 再 patch。
    需 owner 以上。
    """
    assert_project_access(current_user, project_id, "owner")
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE raw_traces
                   SET azimuth_ref = %s
                 WHERE project_id = %s
                   AND target_id  = %s
                   AND deleted_at IS NULL
                """,
                (body.ref, project_id, target_id),
                prepare=False,
            )
            updated = cur.rowcount
    except Exception as e:
        # CHECK 約束破壞 / DB 故障等
        write_audit(
            action="update_azimuth_ref_failed",
            user=current_user, request=request,
            target_type="target", target_ref=target_id, project_id=project_id,
            details={"ref": body.ref, "evidence": body.evidence},
            status_code=500, error_text=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(status_code=500, detail=f"更新方位角基準失敗：{type(e).__name__}: {e}")

    if updated == 0:
        raise HTTPException(status_code=404,
                            detail="找不到符合條件的 raw_traces（target 可能不存在或全部已軟刪）")

    write_audit(
        action="update_azimuth_ref",
        user=current_user, request=request,
        target_type="target", target_ref=target_id, project_id=project_id,
        details={
            "ref": body.ref,
            "evidence": body.evidence,
            "affected_rows": updated,
        },
        status_code=200,
    )
    return {
        "ok": True,
        "project_id": project_id,
        "target_id": target_id,
        "azimuth_ref": body.ref,
        "affected_rows": updated,
        "note": "已更新；audit_logs 已記錄書面依據。",
    }


@router.get("/projects/{project_id}/targets/{target_id}/azimuth-ref")
def get_azimuth_ref_summary(
    project_id: str,
    target_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    查詢某 target 目前的 azimuth_ref 分佈統計。

    回傳格式（避免假定全 target 同一基準，因為 PATCH 只更新「未軟刪」紀錄
    導致同一 target 內可能出現多種值的情況可發生）：
        {
          "total": 100,
          "by_ref": { "magnetic": 80, "unknown": 20 }
        }
    需 viewer 以上。
    """
    assert_project_access(current_user, project_id, "viewer")
    sql = """
    SELECT azimuth_ref, COUNT(*)
      FROM raw_traces
     WHERE project_id = %s
       AND target_id  = %s
       AND deleted_at IS NULL
     GROUP BY azimuth_ref
    """
    by_ref = {}
    total = 0
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (project_id, target_id), prepare=False)
        for r in cur.fetchall():
            by_ref[r[0]] = int(r[1])
            total += int(r[1])
    return {
        "project_id": project_id,
        "target_id": target_id,
        "total": total,
        "by_ref": by_ref,
    }


@router.get(
    "/projects/{project_id}/targets/{target_id}/deleted",
    dependencies=[Depends(require_admin)],
)
def list_deleted_traces(project_id: str, target_id: str):
    """
    列出某 target 已軟刪的 raw_traces（admin only）。
    供管理員檢視「誰刪過什麼」+ 評估是否要 restore。
    """
    sql = """
    SELECT id, start_ts, end_ts, cell_id, cell_addr, deleted_at, deleted_by, delete_reason
      FROM raw_traces
     WHERE project_id = %s
       AND target_id  = %s
       AND deleted_at IS NOT NULL
     ORDER BY deleted_at DESC, id
     LIMIT 1000
    """
    items = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (project_id, target_id), prepare=False)
        for r in cur.fetchall():
            items.append({
                "id": r[0],
                "start_ts": r[1].isoformat() if r[1] else None,
                "end_ts":   r[2].isoformat() if r[2] else None,
                "cell_id":  r[3],
                "cell_addr": r[4],
                "deleted_at":   r[5].isoformat() if r[5] else None,
                "deleted_by":   r[6],
                "delete_reason": r[7],
            })
    return {"total": len(items), "items": items}


# ============================================================
# P2.5-C：Project 層級方位角基準彙總 dashboard
# ============================================================
@router.get("/projects/{project_id}/azimuth-ref-summary")
def get_project_azimuth_ref_summary(
    project_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    一次回傳 project 內所有 target 的 azimuth_ref 分佈 + 最後標註人。

    用於 P2.5-C 法庭防禦性 dashboard：
      - unknown_pct > 0 的 target 需要在法庭前補標
      - last_annotator / last_annotated_at / last_evidence 提供完整 audit trail

    回傳格式：
      {
        "project_id": "demo_case",
        "project_unknown_pct": 42.3,      # 全案 unknown 比例
        "targets": [
          {
            "target_id": "楊云豪",
            "total": 68,
            "by_ref": {"magnetic": 60, "unknown": 8},
            "unknown_pct": 11.8,
            "last_annotator": "admin",
            "last_annotated_at": "2026-05-10T00:53:11+00:00",
            "last_evidence": "NTT DoCoMo 說明書第3頁",
            "last_ref": "magnetic"
          },
          ...
        ]
      }
    需 viewer 以上。
    """
    assert_project_access(current_user, project_id, "viewer")
    ref_sql = """
    SELECT target_id, azimuth_ref, COUNT(*) AS cnt
      FROM raw_traces
     WHERE project_id = %s AND deleted_at IS NULL
     GROUP BY target_id, azimuth_ref
     ORDER BY target_id, azimuth_ref
    """
    annotation_sql = """
    SELECT DISTINCT ON (target_ref)
           target_ref,
           username,
           ts,
           details->>'evidence' AS evidence,
           details->>'ref'      AS ref
      FROM audit_logs
     WHERE project_id = %s AND action = 'update_azimuth_ref'
     ORDER BY target_ref, ts DESC
    """

    targets: dict = {}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(ref_sql, (project_id,), prepare=False)
        for tid, ref, cnt in cur.fetchall():
            if tid not in targets:
                targets[tid] = {"target_id": tid, "by_ref": {}, "total": 0,
                                "last_annotator": None, "last_annotated_at": None,
                                "last_evidence": None, "last_ref": None}
            targets[tid]["by_ref"][ref] = int(cnt)
            targets[tid]["total"] += int(cnt)

        cur.execute(annotation_sql, (project_id,), prepare=False)
        for tid, username, ts, evidence, ref in cur.fetchall():
            if tid in targets:
                targets[tid]["last_annotator"]   = username
                targets[tid]["last_annotated_at"] = ts.isoformat() if ts else None
                targets[tid]["last_evidence"]     = evidence
                targets[tid]["last_ref"]          = ref

    items = []
    for t in sorted(targets.values(), key=lambda x: x["target_id"]):
        total   = t["total"]
        unknown = t["by_ref"].get("unknown", 0)
        t["unknown_pct"] = round(unknown / total * 100, 1) if total else 0.0
        items.append(t)

    all_total   = sum(t["total"] for t in items)
    all_unknown = sum(t["by_ref"].get("unknown", 0) for t in items)

    return {
        "project_id": project_id,
        "project_unknown_pct": round(all_unknown / all_total * 100, 1) if all_total else 0.0,
        "targets": items,
    }


# ============================================================
# P4.3：儲存臨時模式記錄至 DB（臨時→專案轉換）
# ============================================================
class SaveRecordsIn(BaseModel):
    records: List[Dict[str, Any]] = Field(
        ..., description="從 parse-temp 取得的 record list，每筆含 lat/lng/start_ts 等"
    )
    source_note: str = Field(default="converted from temp mode", max_length=255)


@router.post("/projects/{project_id}/targets/{target_id}/save-records")
def save_records(
    project_id: str,
    target_id: str,
    request: Request,
    body: SaveRecordsIn,
    current_user: dict = Depends(get_current_user),
):
    """
    將前端臨時模式解析好的 records 直接寫入 DB（不需重新上傳原始檔案）。

    records 格式與 parse-temp response._records 相同：
      [{ start_ts, end_ts, cell_id, cell_addr, lat, lng, azimuth, ... }]

    注意：
    - 不建立 evidence_file 記錄（無原始檔案可 hash）
    - audit_logs 會記錄此轉換動作，包含 source_note
    - lat/lng 為 None 的記錄仍會寫入（geom = NULL，不在地圖顯示）
    """
    if AUTH_ENABLED and current_user.get("role") != "admin":
        assert_project_access(current_user, project_id, "collaborator")

    db_records: List[Dict[str, Any]] = []
    for r in body.records:
        start_ts = _parse_ts(r.get("start_ts"))
        end_ts   = _parse_ts(r.get("end_ts")) or start_ts
        if not start_ts:
            continue
        db_records.append({
            "project_id":  project_id,
            "target_id":   target_id,
            "start_ts":    start_ts,
            "end_ts":      end_ts,
            "cell_id":     r.get("cell_id"),
            "cell_addr":   r.get("cell_addr"),
            "sector_name": r.get("sector_name"),
            "site_code":   r.get("site_code"),
            "sector_id":   r.get("sector_id"),
            "azimuth":     r.get("azimuth"),
            "lat":         r.get("lat"),
            "lng":         r.get("lng"),
            "accuracy_m":  r.get("accuracy_m"),
        })

    inserted = _insert_records(db_records)

    write_audit(
        action="save_temp_records",
        user=current_user,
        request=request,
        target_type="raw_traces",
        target_ref=target_id,
        project_id=project_id,
        details={
            "inserted":    inserted,
            "total_in":    len(body.records),
            "source_note": body.source_note,
        },
        status_code=200,
    )
    return {
        "ok": True,
        "project_id": project_id,
        "target_id":  target_id,
        "inserted":   inserted,
        "total_in":   len(body.records),
    }
