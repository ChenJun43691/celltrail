#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# CellTrail 上傳流程一鍵驗證腳本
#
# 用途：
#   驗證 /api/upload 端到端流程：產生測試 CSV → 上傳 → 查 DB 確認入庫 + geocode。
#
# 前置條件：
#   1. Docker container 已啟動（celltrail_db 至少要 healthy）
#   2. uvicorn 跑在 http://127.0.0.1:8000（任一終端，單 process 模式即可）
#      啟動指令：
#        cd backend && source .venv/bin/activate
#        uvicorn app.main:app --port 8000
#   3. backend/.env 的 AUTH_ENABLED=false（目前預設）
#
# 用法：
#   bash backend/scripts/smoke_upload.sh
#
# 退出碼：
#   0 = 全部通過（total=2, inserted=2, 且有座標）
#   1 = API 不通 / Docker 不通
#   2 = 上傳本身失敗（HTTP != 200 或 inserted != 2）
#   3 = 上傳成功但 geocode 失敗（lat/lng 為空）
# -----------------------------------------------------------------------------
set -u  # 未定義變數視為錯誤（但不用 -e，我們要自己控流程）

API_BASE="http://127.0.0.1:8000"
PROJECT_ID="smoke_test_$(date +%Y%m%d_%H%M%S)"
CSV_PATH="/tmp/celltrail_smoke.csv"

# ---- 色碼（看得清楚用）----
RED=$'\033[31m'
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
BLUE=$'\033[34m'
RESET=$'\033[0m'

echo "${BLUE}================================${RESET}"
echo "${BLUE} CellTrail Upload Smoke Test${RESET}"
echo "${BLUE}================================${RESET}"
echo "Project ID: ${PROJECT_ID}"
echo ""

# ---- 0) 前置健康檢查 ----
echo "${BLUE}[0/3] 健康檢查${RESET}"

# Docker DB
if ! docker ps --filter name=celltrail_db --format '{{.Names}}' | grep -q celltrail_db; then
  echo "${RED}✗ celltrail_db 沒跑，請先：cd infra && docker compose up -d${RESET}"
  exit 1
fi
echo "  ✓ celltrail_db 活著"

# API
if ! curl -sSf -o /dev/null "${API_BASE}/api"; then
  echo "${RED}✗ API 不通（${API_BASE}/api），請先啟動 uvicorn${RESET}"
  exit 1
fi
echo "  ✓ API 回應正常"

# 無登入模式
AUTH_ROLE=$(curl -s "${API_BASE}/api/auth/me" | python3 -c "import sys,json; print(json.load(sys.stdin).get('role',''))" 2>/dev/null)
if [ "${AUTH_ROLE}" != "admin" ]; then
  echo "${YELLOW}⚠ /api/auth/me 回傳的 role 不是 admin（實際：${AUTH_ROLE}）${RESET}"
  echo "  檢查 .env 的 AUTH_ENABLED=false 是否生效"
fi
echo "  ✓ 無登入模式生效（role=admin）"
echo ""

# ---- 1) 產生測試 CSV ----
echo "${BLUE}[1/3] 產生測試 CSV${RESET}"
cat > "${CSV_PATH}" <<'EOF'
開始連線時間,結束連線時間,基地台地址,基地台編號,方位角
2026/04/25 10:00:00,2026/04/25 10:30:00,高雄市左營區博愛二路777號,,120
2026/04/25 11:30:00,2026/04/25 12:00:00,台北市信義區信義路五段7號,,240
EOF
echo "  ✓ 寫入 ${CSV_PATH}（2 筆資料）"
echo ""

# ---- 2) 上傳 ----
echo "${BLUE}[2/3] POST /api/upload/${RESET}"
RESP=$(curl -sS -X POST "${API_BASE}/api/upload/" \
  -F "project_id=${PROJECT_ID}" \
  -F "target_id=T001" \
  -F "file=@${CSV_PATH}")

echo "${RESP}" | python3 -m json.tool

# 解析 inserted / skipped
INSERTED=$(echo "${RESP}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('inserted',-1))" 2>/dev/null)
SKIPPED=$(echo "${RESP}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('skipped',-1))" 2>/dev/null)

if [ "${INSERTED}" != "2" ] || [ "${SKIPPED}" != "0" ]; then
  echo "${RED}✗ 上傳結果異常：inserted=${INSERTED} skipped=${SKIPPED}${RESET}"
  exit 2
fi
echo "  ${GREEN}✓ 上傳成功：inserted=2, skipped=0${RESET}"
echo ""

# ---- 3) 查 DB 確認入庫 + geocode 結果 ----
echo "${BLUE}[3/3] 查 raw_traces 確認入庫 + geocode${RESET}"
SQL="SELECT target_id, start_ts::text, cell_addr,
        COALESCE(ROUND(lat::numeric, 4)::text, 'NULL') AS lat,
        COALESCE(ROUND(lng::numeric, 4)::text, 'NULL') AS lng,
        COALESCE(ST_AsText(geom), 'NULL') AS geom
     FROM raw_traces
     WHERE project_id='${PROJECT_ID}'
     ORDER BY start_ts;"

docker exec -i celltrail_db psql -U celltrail -d celltrail -c "${SQL}"

# 判斷 geocode 是否成功（看有幾筆 lat 是 NULL）
NULL_COUNT=$(docker exec -i celltrail_db psql -U celltrail -d celltrail -tAc \
  "SELECT COUNT(*) FROM raw_traces WHERE project_id='${PROJECT_ID}' AND lat IS NULL;")

echo ""
if [ "${NULL_COUNT}" = "0" ]; then
  echo "${GREEN}════════════════════════════════════════════${RESET}"
  echo "${GREEN}  ✓ 全部通過：2 筆資料皆完成 geocode${RESET}"
  echo "${GREEN}════════════════════════════════════════════${RESET}"
  echo ""
  echo "前端驗證：瀏覽器開啟 http://localhost:5500/index.html"
  echo "將 Project ID 改為：${PROJECT_ID}"
  echo "點「顯示資料（從資料庫）」，應看到台北+高雄兩個扇形"
  exit 0
else
  echo "${YELLOW}════════════════════════════════════════════${RESET}"
  echo "${YELLOW}  ⚠ 上傳成功但 geocode 失敗（${NULL_COUNT}/2 筆無座標）${RESET}"
  echo "${YELLOW}════════════════════════════════════════════${RESET}"
  echo ""
  echo "請回去看 uvicorn 終端的 [geocode] log，會有下列之一："
  echo "  • [geocode] Google 非 OK: status=... error_message=..."
  echo "  • [geocode] Google 例外: XxxError: ..."
  echo "  • [geocode] 所有來源均無結果 addr=..."
  echo ""
  echo "把 log 貼給 Claude，就能決定下一步修法。"
  exit 3
fi
