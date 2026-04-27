#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# CellTrail：套用 P0 + P1 schema 變更（audit_logs 表 + raw_traces.deleted_at）
#
# 為什麼單獨拉一支腳本：
#   schema.sql 是「冪等」設計（CREATE TABLE IF NOT EXISTS / ALTER ... IF NOT EXISTS），
#   所以對既有 DB 重跑也安全 —— 但 reviewer 還是要看到一個「明確套用」的入口。
#
# 用法：
#   bash backend/scripts/apply_schema_p0p1.sh
# -----------------------------------------------------------------------------
set -u

GREEN=$'\033[32m'; RED=$'\033[31m'; BLUE=$'\033[34m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'

echo "${BLUE}[1/3] 套用 schema.sql 到 Docker DB${RESET}"
docker exec -i celltrail_db psql -U celltrail -d celltrail \
  < "$(dirname "$0")/../app/db/schema.sql"
RC=$?
if [ $RC -ne 0 ]; then
  echo "${RED}✗ schema.sql 套用失敗（exit ${RC}），請看上面 log${RESET}"
  exit $RC
fi

echo ""
echo "${BLUE}[2/3] 驗證 audit_logs 表結構${RESET}"
# 註：不用 psql 的 \d meta-command，因為 -c 多行模式下會被當成 SQL 解析（會噴 syntax error at "\"）。
# 改查 information_schema.columns 與 PostgreSQL 系統 catalog —— 標準 SQL，跨環境穩定。
docker exec -i celltrail_db psql -U celltrail -d celltrail -c "
SELECT column_name, data_type, is_nullable
  FROM information_schema.columns
 WHERE table_name = 'audit_logs'
 ORDER BY ordinal_position;
" 2>&1 | head -40

echo ""
echo "${BLUE}[3/3] 驗證 raw_traces 新欄位${RESET}"
docker exec -i celltrail_db psql -U celltrail -d celltrail -c "
SELECT column_name, data_type
  FROM information_schema.columns
 WHERE table_name='raw_traces'
   AND column_name IN ('deleted_at','deleted_by','delete_reason')
 ORDER BY column_name;
"

echo ""
echo "${GREEN}════════════════════════════════════════════${RESET}"
echo "${GREEN}  ✓ schema 套用完成${RESET}"
echo "${GREEN}════════════════════════════════════════════${RESET}"
echo ""
echo "下一步："
echo "  1) 重啟 uvicorn（Ctrl+C → uvicorn app.main:app --port 8000）"
echo "  2) 跑 smoke test： bash backend/scripts/smoke_audit.sh"
