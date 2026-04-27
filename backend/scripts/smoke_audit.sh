#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# CellTrail Smoke Test：驗證 P0+P1 改動端到端
#
# 步驟：
#   1) 上傳一份小 CSV → 預期 audit_logs 多一筆 action='upload'
#   2) 軟刪該 target → 預期 raw_traces.deleted_at 非 NULL + audit_logs 多 'delete_target'
#   3) GET /api/map-layers → 預期回傳 features=[]（被軟刪過濾掉）
#   4) admin 還原 → 預期 deleted_at=NULL + audit_logs 多 'restore_target'
#   5) 查 GET /api/audit/logs → 預期至少 3 筆 audit
# -----------------------------------------------------------------------------
set -u

API=${API:-http://127.0.0.1:8000}
PROJECT="audit_smoke_$(date +%H%M%S)"
TARGET="T_AUDIT"

GREEN=$'\033[32m'; RED=$'\033[31m'; BLUE=$'\033[34m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'

# ---- 0) 檢查 uvicorn 是否在跑 ----
if ! curl -sS "${API}/api/health/" > /dev/null 2>&1; then
  echo "${RED}✗ uvicorn 未啟動（${API}/api/health/ 無回應）${RESET}"
  echo "  請先在另一個終端跑： uvicorn app.main:app --port 8000"
  exit 1
fi
echo "${GREEN}✓ uvicorn alive${RESET}"

# ---- 1) 準備一份最小 CSV（2 筆，含可 geocode 的高雄地址） ----
TMP=$(mktemp -d)
CSV="${TMP}/sample.csv"
cat > "${CSV}" <<'CSV'
開始連線時間,結束連線時間,基地台位址,基地台ID,方位角
2026-04-26 10:00:00,2026-04-26 10:05:00,高雄市苓雅區三多四路117號,001,90
2026-04-26 10:05:30,2026-04-26 10:07:00,高雄市前鎮區中山二路2號,002,180
CSV

echo ""
echo "${BLUE}[1/5] 上傳 CSV（project=${PROJECT}, target=${TARGET}）${RESET}"
UPLOAD=$(curl -sS -X POST "${API}/api/upload/" \
  -F "project_id=${PROJECT}" -F "target_id=${TARGET}" -F "file=@${CSV}")
echo "  ${UPLOAD}" | head -c 500
echo ""

# ---- 2) 軟刪 ----
echo ""
echo "${BLUE}[2/5] 軟刪 target${RESET}"
DEL=$(curl -sS -X DELETE "${API}/api/projects/${PROJECT}/targets/${TARGET}" \
  -H "Content-Type: application/json" \
  -d '{"reason":"smoke test 驗收"}')
echo "  ${DEL}"

# ---- 3) map-layers 應該空了 ----
echo ""
echo "${BLUE}[3/5] map-layers 應為空（軟刪過濾驗證）${RESET}"
LAYERS=$(curl -sS "${API}/api/projects/${PROJECT}/map-layers")
N_FEATURES=$(echo "${LAYERS}" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d.get("features",[])))')
if [ "${N_FEATURES}" = "0" ]; then
  echo "  ${GREEN}✓ features=0（軟刪生效）${RESET}"
else
  echo "  ${RED}✗ features=${N_FEATURES}（應為 0）${RESET}"
fi

# ---- 4) 還原 ----
echo ""
echo "${BLUE}[4/5] admin 還原${RESET}"
RES=$(curl -sS -X POST "${API}/api/projects/${PROJECT}/targets/${TARGET}/restore" \
  -H "Content-Type: application/json" \
  -d '{"reason":"smoke test 還原"}')
echo "  ${RES}"

# ---- 5) 查 audit log ----
echo ""
echo "${BLUE}[5/5] 查 audit logs（應至少 3 筆）${RESET}"
LOGS=$(curl -sS "${API}/api/audit/logs?project_id=${PROJECT}&page_size=50")
N_LOGS=$(echo "${LOGS}" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("total",0))')
echo "  total=${N_LOGS}"

# 條列 actions
# 註：先把 dict 取值拉到 local 變數，避免 f-string 內 "[\"...\"]" 帶反斜線。
#     shell single-quote 內 `\` 不是 shell 轉義字元，會原樣丟給 python，
#     導致 f-string 把 `\"` 看成 line continuation character → SyntaxError。
echo "  actions:"
echo "${LOGS}" | python3 -c '
import sys, json
d = json.load(sys.stdin)
for it in d.get("items", []):
    ts = it["ts"][:19]
    act = it["action"]
    sc = it.get("status_code")
    u = it.get("username")
    ip = it.get("ip")
    print(f"    [{ts}] {act:24s} status={sc}  by={u} ip={ip}")
'

echo ""
echo "${BLUE}[+] 驗 evidence_files（P2 全 hash）${RESET}"
EV=$(curl -sS "${API}/api/projects/${PROJECT}/evidence-files")
N_EV=$(echo "${EV}" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("total",0))')
SHA_FIRST=$(echo "${EV}" | python3 -c '
import sys,json
d=json.load(sys.stdin)
items=d.get("items") or []
print(items[0]["sha256_full"] if items else "")
')
echo "  evidence_files total=${N_EV}"
echo "  sha256_full=${SHA_FIRST}"
if [ "${N_EV}" = "1" ] && [ ${#SHA_FIRST} -eq 64 ]; then
  echo "  ${GREEN}✓ evidence 指紋封存生效（SHA-256 共 64 字元）${RESET}"
else
  echo "  ${YELLOW}⚠ evidence_files 結果異常 (total=${N_EV} hash 長度=${#SHA_FIRST})${RESET}"
fi

echo ""
echo "${BLUE}[+] 驗 PDF 報告匯出${RESET}"
PDF_OUT="${TMP}/report.pdf"
HTTP_CODE=$(curl -sS -o "${PDF_OUT}" -w "%{http_code}" \
  "${API}/api/projects/${PROJECT}/evidence-report")
SIZE=$(wc -c < "${PDF_OUT}")
HEAD4=$(head -c 4 "${PDF_OUT}")
if [ "${HTTP_CODE}" = "200" ] && [ "${HEAD4}" = "%PDF" ]; then
  echo "  ${GREEN}✓ 報告下載成功 size=${SIZE}B（PDF magic=${HEAD4}）${RESET}"
else
  echo "  ${YELLOW}⚠ 報告下載 http=${HTTP_CODE} size=${SIZE}B head='${HEAD4}'${RESET}"
  echo "    （若 reportlab 尚未安裝，請先 pip install reportlab==4.2.5）"
fi

echo ""
if [ "${N_LOGS}" -ge 3 ]; then
  echo "${GREEN}════════════════════════════════════════════${RESET}"
  echo "${GREEN}  ✓ Smoke test 通過：audit chain 完整${RESET}"
  echo "${GREEN}════════════════════════════════════════════${RESET}"
  rm -rf "${TMP}"
  exit 0
else
  echo "${RED}✗ audit logs 不足 3 筆（實際 ${N_LOGS}），請查 uvicorn log${RESET}"
  rm -rf "${TMP}"
  exit 2
fi
