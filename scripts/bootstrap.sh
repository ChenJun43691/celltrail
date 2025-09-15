

#!/usr/bin/env bash
# CellTrail bootstrap & environment quick-check script
# Usage:
#   chmod +x scripts/bootstrap.sh
#   ./scripts/bootstrap.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
cd "$ROOT_DIR"

echo "== CellTrail environment quick check =="
echo "Project root: $ROOT_DIR"
echo

FAIL=()

check_dir () {
  local d="$1"
  if [[ -d "$d" ]]; then
    echo "✔ dir  $d"
  else
    echo "✘ dir  $d   (MISSING)"
    FAIL+=("$d/")
  fi
}

check_file () {
  local f="$1"
  if [[ -f "$f" ]]; then
    echo "✔ file $f"
  else
    echo "✘ file $f   (MISSING)"
    FAIL+=("$f")
  fi
}

check_cmd () {
  local c="$1"
  if command -v "$c" >/dev/null 2>&1; then
    echo "✔ cmd  $c"
  else
    echo "✘ cmd  $c   (NOT FOUND)"
    FAIL+=("$c (command)")
  fi
}

echo "[1/4] Checking required directories..."
check_dir "infra"
check_dir "backend"
check_dir "backend/app"
check_dir "backend/app/api"
check_dir "backend/app/services"
check_dir "backend/app/db"
check_dir "frontend"
check_dir "data"
check_dir "docs"
echo

echo "[2/4] Checking required files..."
check_file "infra/docker-compose.yml"
check_file "backend/requirements.txt"
check_file "backend/app/main.py"
check_file "backend/app/api/health.py"
check_file "backend/app/api/upload.py"
check_file "backend/app/db/schema.sql"
echo

echo "[3/4] Checking essential commands..."
check_cmd "docker"
check_cmd "python3"
check_cmd "node" || true   # 前端稍後初始化，先不當作致命錯誤
check_cmd "psql" || true   # 可能未安裝於主機，將改用 docker exec
echo

echo "[4/4] Bringing up infra (PostGIS + Redis) with Docker Compose..."
if [[ -f "infra/docker-compose.yml" ]]; then
  (cd infra && docker compose up -d)
else
  echo "✘ Missing infra/docker-compose.yml; cannot start infra."
  FAIL+=("docker-compose")
fi

echo "Waiting for Postgres container to become healthy (celltrail_db)..."
for i in {1..30}; do
  status="$(docker inspect -f '{{.State.Health.Status}}' celltrail_db 2>/dev/null || true)"
  if [[ "$status" == "healthy" ]]; then
    echo "✔ Postgres is healthy."
    break
  fi
  sleep 2
  if [[ $i -eq 30 ]]; then
    echo "✘ Postgres did not become healthy in time."
    FAIL+=("postgres (health)")
  fi
done

echo "Ensuring PostGIS extension is enabled..."
if docker exec -i celltrail_db psql -U celltrail -d celltrail -c "CREATE EXTENSION IF NOT EXISTS postgis;" >/dev/null 2>&1; then
  echo "✔ PostGIS extension ready."
else
  echo "✘ Failed to enable PostGIS extension."
  FAIL+=("postgis (extension)")
fi

echo "Checking Redis container (celltrail_redis)..."
if docker ps --format '{{.Names}}' | grep -q '^celltrail_redis$'; then
  echo "✔ Redis is running."
else
  echo "✘ Redis container not running."
  FAIL+=("redis (container)")
fi

echo
echo "Next step (manual): start the API server locally:"
echo "  cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
echo "  uvicorn app.main:app --reload --port 8000"
echo "Then open: http://localhost:8000/api/health/"
echo

if [[ ${#FAIL[@]} -eq 0 ]]; then
  echo "✅ All checks passed. Your environment looks good!"
  exit 0
else
  echo "⚠ Some checks failed. Please resolve the following items:"
  printf ' - %s\n' "${FAIL[@]}"
  exit 1
fi