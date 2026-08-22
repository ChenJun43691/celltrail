#!/usr/bin/env bash
# scripts/sync_agents_md.sh
# ---------------------------------------------------------------------------
# 由 CLAUDE.md 產生 AGENTS.md（給 Codex 用的同一份專案說明）。
#
# 為什麼需要這支：
#   兩份文件內容原本 100% 相同、只差「Claude→Codex」的稱謂替換，卻各自手動維護
#   —— 結果 2026-07-08～08-22 之間 AGENTS.md 少了一整輪紀錄（包含七-11「OSM 回傳
#   錯誤座標」這條最危險的警告）。讓 Codex 讀到一份缺少關鍵風險警示的說明，
#   後果不是文件不整齊，而是它可能重蹈已經踩過的坑。
#   故改為「單一來源 + 機械產生」，drift 在架構上不可能發生。
#
# 用法：
#   bash scripts/sync_agents_md.sh          # 產生／更新 AGENTS.md
#   bash scripts/sync_agents_md.sh --check  # 只檢查是否同步（CI 用；不同步回 exit 1）
#
# 例外區：CLAUDE.md 中夾在 <!-- sync:verbatim-start --> / <!-- sync:verbatim-end -->
#         之間的內容原樣保留、不做替換（用於同時提到兩個檔名的段落）。
#         這對哨兵標記本身也成立 —— 產出的 AGENTS.md 不含這兩行。
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
SRC="$ROOT/CLAUDE.md"
DST="$ROOT/AGENTS.md"

[ -f "$SRC" ] || { echo "✗ 找不到 $SRC" >&2; exit 2; }

GENERATED=$(SRC="$SRC" python3 - <<'PYEOF'
import os, re, sys

src = open(os.environ["SRC"], encoding="utf-8").read()

# 稱謂替換。順序有意義：先處理最長的字面量，避免被較短的規則先吃掉。
RULES = [
    ("# CLAUDE.md", "# AGENTS.md"),
    ("This file provides guidance to Claude Code (claude.ai/code)",
     "This file provides guidance to Codex (Codex.ai/code)"),
    ("Claude Code", "Codex"),
    ("Claude", "Codex"),
]

out, pos = [], 0
# 逐段處理，verbatim 區間原樣搬過去（連哨兵一起丟掉，不留在產出裡）。
for m in re.finditer(
    r"<!-- sync:verbatim-start -->\n(.*?)<!-- sync:verbatim-end -->\n?",
    src, re.S,
):
    chunk = src[pos:m.start()]
    for a, b in RULES:
        chunk = chunk.replace(a, b)
    out.append(chunk)
    out.append(m.group(1))
    pos = m.end()

tail = src[pos:]
for a, b in RULES:
    tail = tail.replace(a, b)
out.append(tail)

sys.stdout.write("".join(out))
PYEOF
)

if [ "${1:-}" = "--check" ]; then
  if [ -f "$DST" ] && [ "$GENERATED" = "$(cat "$DST")" ]; then
    echo "✓ AGENTS.md 與 CLAUDE.md 同步"
    exit 0
  fi
  echo "✗ AGENTS.md 與 CLAUDE.md 不同步 —— 請執行 bash scripts/sync_agents_md.sh" >&2
  exit 1
fi

printf '%s' "$GENERATED" > "$DST"
echo "✓ 已由 CLAUDE.md 產生 AGENTS.md（$(wc -l < "$DST" | tr -d ' ') 行）"
