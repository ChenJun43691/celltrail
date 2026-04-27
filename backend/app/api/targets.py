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
from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.db.session import get_conn
from app.security import get_current_user, require_admin
from app.services.audit import write_audit

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
    """還原軟刪的 target 紀錄；強制填還原理由。"""
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
    """
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


@router.get(
    "/projects/{project_id}/targets/{target_id}/azimuth-ref",
    dependencies=[Depends(get_current_user)],
)
def get_azimuth_ref_summary(project_id: str, target_id: str):
    """
    查詢某 target 目前的 azimuth_ref 分佈統計。

    回傳格式（避免假定全 target 同一基準，因為 PATCH 只更新「未軟刪」紀錄
    導致同一 target 內可能出現多種值的情況可發生）：
        {
          "total": 100,
          "by_ref": { "magnetic": 80, "unknown": 20 }
        }
    """
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
