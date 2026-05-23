#!/usr/bin/env bash
# frontend/tests/mint-token.sh
# ---------------------------------------------------------------------------
# 用後端的 SECRET_KEY 鑄一個 admin token 給 smoke test 用。
# 等同登入該帳號，不需密碼也不寫 DB；僅 dev 環境使用。
#
# 用法：
#   bash mint-token.sh <admin-username>
#
# 常見：
#   export CT_SMOKE_TOKEN=$(bash mint-token.sh CIDadmin)
#   npm test
#
# 前置：backend/.venv 已建、backend/.env 已設 SECRET_KEY。
# ---------------------------------------------------------------------------
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <admin-username>" >&2
  exit 2
fi

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT/backend"

if [ ! -f .venv/bin/activate ]; then
  echo "✗ 找不到 backend/.venv，請先建 venv（見 CLAUDE.md 第三節）" >&2
  exit 2
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# 用 env var 傳 username，避免單引號注入；import app.main 觸發 .env 載入。
CT_MINT_USER="$1" python -c "
import os, app.main
from app.security import create_access_token
print(create_access_token({'sub': os.environ['CT_MINT_USER']}))
"
