#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# CellTrail：修復 requests 套件損壞（PEP 420 namespace package 空殼）
#
# 症狀：
#   uvicorn log 出現
#     [geocode] Google 例外: AttributeError: module 'requests' has no attribute 'get'
#   原因是 site-packages 內的 `requests/` 資料夾被「砍乾淨但保留資料夾本身」，
#   Python 3 仍把它當成 implicit namespace package 載入，導致 attribute 全部消失。
#
# 此腳本流程：
#   1) 確認在 backend/.venv 環境（避免污染系統 Python）
#   2) 把 requests 殘骸（資料夾 + dist-info）整個刪除
#   3) --force-reinstall 重灌 requests
#   4) 驗證：印 __version__ / __file__ / hasattr(get)
#   5) 保險：requirements.txt 整包 reinstall（可選）
#
# 用法（在 backend 目錄下執行）：
#   bash scripts/fix_requests.sh           # 只修 requests
#   bash scripts/fix_requests.sh --all     # 修 requests + reinstall 全部依賴
# -----------------------------------------------------------------------------
set -u

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BLUE=$'\033[34m'; RESET=$'\033[0m'

# ---- 0) 環境檢查 ----
echo "${BLUE}[0/4] 環境檢查${RESET}"

# 必須在 venv 中
if [ -z "${VIRTUAL_ENV:-}" ]; then
  echo "${RED}✗ 偵測不到 VIRTUAL_ENV。請先：source backend/.venv/bin/activate${RESET}"
  exit 1
fi
echo "  ✓ VIRTUAL_ENV=${VIRTUAL_ENV}"

# 必須是 backend/.venv（避免誤刪到別的 venv）
case "${VIRTUAL_ENV}" in
  */CellTrail/backend/.venv)
    echo "  ✓ 確認是 CellTrail backend 的 venv"
    ;;
  *)
    echo "${YELLOW}⚠ VIRTUAL_ENV 路徑看起來不像 CellTrail backend，請手動確認後再繼續${RESET}"
    read -r -p "  仍要繼續？(y/N) " ans
    [ "${ans}" = "y" ] || [ "${ans}" = "Y" ] || { echo "已中止"; exit 1; }
    ;;
esac

PY_VER=$(python -c 'import sys;print(f"python{sys.version_info.major}.{sys.version_info.minor}")')
SITE="${VIRTUAL_ENV}/lib/${PY_VER}/site-packages"
echo "  ✓ site-packages: ${SITE}"
echo ""

# ---- 1) 顯示損壞前狀態 ----
echo "${BLUE}[1/4] 重裝前診斷${RESET}"
python - <<'PY'
import importlib, importlib.util
spec = importlib.util.find_spec("requests")
if spec is None:
    print("  · find_spec(requests) → None（連空殼都沒有）")
else:
    print(f"  · spec.origin   = {spec.origin}")
    print(f"  · spec.submodule_search_locations = {spec.submodule_search_locations}")
try:
    import requests
    print(f"  · __file__      = {getattr(requests, '__file__', None)}")
    print(f"  · __version__   = {getattr(requests, '__version__', '<missing>')}")
    print(f"  · has get?      = {hasattr(requests, 'get')}")
except Exception as e:
    print(f"  · import requests 失敗：{type(e).__name__}: {e}")
PY
echo ""

# ---- 2) 砍殘骸（連同 requests 整族相依一起，避免 pip uninstall-no-record-file 連鎖錯）----
echo "${BLUE}[2/4] 移除損壞的 requests + 相依族${RESET}"
# 為什麼連 urllib3/idna/certifi/charset_normalizer 一起砍？
# 觀察：venv 被某次操作把這些主資料夾砍光只留 dist-info，pip 反裝時會抱怨
#       「Cannot uninstall urllib3 None ... no RECORD file was found」
# 這時 pip 不會自動修復，要把空殼資料夾與 dist-info 一起清掉，pip 才會走「全新安裝」路徑。
DEPS=(requests urllib3 idna certifi charset_normalizer)
for pkg in "${DEPS[@]}"; do
  for path in "${SITE}/${pkg}" "${SITE}/${pkg}-"*.dist-info "${SITE}/${pkg}-"*.egg-info; do
    if [ -e "${path}" ]; then
      echo "  · 刪除：${path}"
      rm -rf "${path}"
    fi
  done
done
echo "  ✓ 殘骸清除完成"
echo ""

# ---- 3) 重裝 ----
echo "${BLUE}[3/4] 重裝 requests${RESET}"
if [ "${1:-}" = "--all" ]; then
  REQ_FILE="$(dirname "$0")/../requirements.txt"
  if [ -f "${REQ_FILE}" ]; then
    echo "  · --all 模式：跑 pip install --force-reinstall -r requirements.txt"
    python -m pip install --force-reinstall --no-deps -r "${REQ_FILE}"
  else
    echo "${YELLOW}  ⚠ 找不到 requirements.txt（${REQ_FILE}），改只裝 requests${RESET}"
    python -m pip install --force-reinstall 'requests>=2.31'
  fi
else
  python -m pip install --force-reinstall 'requests>=2.31'
fi

if [ $? -ne 0 ]; then
  echo "${RED}✗ pip install 失敗，請看上面 log${RESET}"
  exit 2
fi
echo ""

# ---- 4) 驗證 ----
echo "${BLUE}[4/4] 重裝後驗證${RESET}"
python - <<'PY'
import sys
try:
    import requests
except Exception as e:
    print(f"  ✗ 仍然 import 失敗：{type(e).__name__}: {e}")
    sys.exit(3)

ok = hasattr(requests, "get") and hasattr(requests, "post")
print(f"  · __version__ = {requests.__version__}")
print(f"  · __file__    = {requests.__file__}")
print(f"  · has get?    = {hasattr(requests, 'get')}")
print(f"  · has post?   = {hasattr(requests, 'post')}")
if not ok:
    print("  ✗ requests.get / requests.post 仍缺，重裝失敗")
    sys.exit(3)
print("  ✓ requests 工作正常")
PY
RC=$?
echo ""

if [ ${RC} -eq 0 ]; then
  echo "${GREEN}════════════════════════════════════════════${RESET}"
  echo "${GREEN}  ✓ requests 修復完成${RESET}"
  echo "${GREEN}════════════════════════════════════════════${RESET}"
  echo ""
  echo "下一步："
  echo "  1) Ctrl+C 停掉 uvicorn，重新啟動（不要 --reload）："
  echo "       uvicorn app.main:app --port 8000"
  echo "  2) 另開終端跑："
  echo "       bash backend/scripts/smoke_upload.sh"
  echo "  3) 預期 exit code 0，DB 內 lat/lng 不再 NULL"
  exit 0
else
  echo "${RED}✗ 驗證失敗，請把上面 log 貼回對話${RESET}"
  exit ${RC}
fi
