#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# CellTrail：重建 .venv（用於套件損壞時的核彈級救援）
#
# 為什麼要這支腳本：
#   2026-04-26 遭遇連環套件殘缺事故（PIL/_version.py 不見、_pytest/_code/__init__.py 不見、
#   pygments.formatters.terminal 不見），原因是舊 venv 與 requirements.txt 長期飄離
#   （某些套件是手動 pip install 的孤兒，從未寫進 requirements.txt）。
#   修法是「乾淨重建 venv」—— 這支腳本把當天的救援步驟做成冪等流程。
#
# 安全機制：
#   - 舊 .venv 會先改名為 .venv.broken_YYYYMMDD_HHMMSS，不直接刪除
#   - 新 venv 失敗時，可手動 `mv .venv.broken_<timestamp> .venv` 復原
#   - 不會動到 .env、原始碼、DB schema 任何一個檔
#
# 用法（從 backend/ 目錄）：
#   bash scripts/rebuild_venv.sh
# 或從專案根：
#   bash backend/scripts/rebuild_venv.sh
# -----------------------------------------------------------------------------
set -u

GREEN=$'\033[32m'; RED=$'\033[31m'; BLUE=$'\033[34m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'

# ---- 0) 定位 backend 目錄 ----
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${BACKEND_DIR}"
echo "${BLUE}[0/6] 工作目錄：${BACKEND_DIR}${RESET}"

if [ ! -f "requirements.txt" ]; then
  echo "${RED}✗ 找不到 requirements.txt，請確認你在 CellTrail/backend 目錄下${RESET}"
  exit 1
fi

# ---- 1) 偵測現役 Python ----
PY_BIN="${PYTHON:-python3}"
PY_VER=$("${PY_BIN}" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "")
if [ -z "${PY_VER}" ]; then
  echo "${RED}✗ 找不到 ${PY_BIN}${RESET}"
  exit 2
fi
echo "${BLUE}[1/6] 將使用 ${PY_BIN} (Python ${PY_VER}) 重建 venv${RESET}"

if [ "${PY_VER}" != "3.13" ] && [ "${PY_VER}" != "3.12" ]; then
  echo "${YELLOW}⚠ 建議 Python 3.12 或 3.13；目前是 ${PY_VER}，可能與 requirements 不相容${RESET}"
  read -p "  仍要繼續？[y/N] " ans
  [ "${ans}" = "y" ] || [ "${ans}" = "Y" ] || exit 3
fi

# ---- 2) 備份舊 venv（若有） ----
if [ -d ".venv" ]; then
  STAMP=$(date +%Y%m%d_%H%M%S)
  BACKUP=".venv.broken_${STAMP}"
  echo ""
  echo "${BLUE}[2/6] 備份舊 .venv → ${BACKUP}${RESET}"
  mv .venv "${BACKUP}"
  echo "  ${GREEN}✓ 舊 venv 已保留為 ${BACKUP}${RESET}"
  echo "  ${YELLOW}（驗證新 venv OK 後可手動刪：rm -rf ${BACKUP}）${RESET}"
else
  echo ""
  echo "${BLUE}[2/6] 無舊 .venv 需備份，直接建立${RESET}"
fi

# ---- 3) 建立新 venv ----
echo ""
echo "${BLUE}[3/6] 建立新 venv${RESET}"
"${PY_BIN}" -m venv .venv
if [ ! -f ".venv/bin/python" ]; then
  echo "${RED}✗ venv 建立失敗${RESET}"
  exit 4
fi
echo "  ${GREEN}✓ .venv 建立完成${RESET}"

# 啟用 venv（從這行起所有 pip 都進新 venv）
# shellcheck disable=SC1091
source .venv/bin/activate

# ---- 4) 升級 pip / setuptools / wheel ----
echo ""
echo "${BLUE}[4/6] 升級 pip / setuptools / wheel${RESET}"
pip install --quiet --upgrade pip setuptools wheel
echo "  ${GREEN}✓ pip $(pip --version | awk '{print $2}')${RESET}"

# ---- 5) 安裝 requirements.txt ----
echo ""
echo "${BLUE}[5/6] 安裝 requirements.txt（含 pytest==8.3.3）${RESET}"
pip install -r requirements.txt
RC=$?
if [ $RC -ne 0 ]; then
  echo "${RED}✗ requirements 安裝失敗（exit ${RC}）${RESET}"
  echo "  舊 venv 仍保留為 .venv.broken_*，可手動還原"
  exit 5
fi

# ---- 6) 體檢：pip check + 關鍵 import + pytest 演練 ----
echo ""
echo "${BLUE}[6/6] 體檢${RESET}"
echo -n "  pip check ... "
if pip check > /dev/null 2>&1; then
  echo "${GREEN}✓${RESET}"
else
  echo "${RED}✗（依賴衝突，請看：pip check）${RESET}"
fi

echo -n "  關鍵 import (PIL, reportlab, fastapi, psycopg, pytest) ... "
if python -c "
from PIL import Image
import reportlab
import fastapi
import psycopg
import pytest
" 2>/dev/null; then
  echo "${GREEN}✓${RESET}"
else
  echo "${RED}✗${RESET}"
fi

echo -n "  pytest dry-run（collect-only）... "
if pytest --collect-only -q app/tests/test_audit.py app/tests/test_evidence.py > /dev/null 2>&1; then
  echo "${GREEN}✓${RESET}"
else
  echo "${YELLOW}⚠（測試蒐集失敗，請手動 pytest 看詳細錯誤）${RESET}"
fi

echo ""
echo "${GREEN}════════════════════════════════════════════${RESET}"
echo "${GREEN}  ✓ venv 重建完成${RESET}"
echo "${GREEN}════════════════════════════════════════════${RESET}"
echo ""
echo "下一步建議："
echo "  1) 跑 pytest 全測試：  pytest app/tests/ -v"
echo "  2) 啟 uvicorn：        uvicorn app.main:app --port 8000"
echo "  3) 跑端到端 smoke：    bash scripts/smoke_audit.sh"
echo ""
echo "備份的舊 venv 在：.venv.broken_*  （磁碟空間 ~500MB；驗證新 venv OK 後可刪）"
