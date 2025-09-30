# app/api/targets.py
from fastapi import APIRouter, HTTPException, Depends
from app.security import get_current_user
from app.db.session import pool  # 用連線池

router = APIRouter()

@router.delete("/projects/{project_id}/targets/{target_id}",
               dependencies=[Depends(get_current_user)])  # ← 登入即可刪除
def delete_target(project_id: str, target_id: str):
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM raw_traces WHERE project_id = %s AND target_id = %s",
                (project_id, target_id),
            )
            deleted = cur.rowcount
            conn.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刪除失敗：{type(e).__name__}: {e}")

    if deleted == 0:
        raise HTTPException(status_code=404, detail="Target 不存在或已刪除")

    return {"ok": True, "deleted": deleted, "project_id": project_id, "target_id": target_id}