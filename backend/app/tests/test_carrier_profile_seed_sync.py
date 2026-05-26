"""
schema.sql 內 carrier_profiles 種子 mapping_json 與 ingest._RAW2CANON 的
同步守護（2026-05-26，WAKE_UP_TODO #3）。

Background
==========
ingest.py 的 _RAW2CANON 是「電信業者欄名 → canonical 欄名」對照表的
code-side baseline；schema.sql 的 carrier_profiles 種子 INSERT 是同一份
資料在 DB 端的初始值。兩者本應永遠同步：
  - 加新別名到 _RAW2CANON 但忘了改 schema.sql → 新 DB 部署時種子缺欄，
    要靠 carrier_profile.get_active_header_map() 內的「code 補空缺」
    merge fallback 才不會壞掉 —— 但這條 fallback 是安全網不是設計，
    哪天有人把它拿掉就出事。
  - 反向同理：改 schema.sql 但忘改 code，DB 端有的 key 不會被 ingest
    當「應該存在的別名」對待（影響行為更隱微）。

本檔在 CI 階段就抓 drift，逼開發者同時更新兩處（或主動決定不同步並
解釋原因）。

非測試對象
==========
- 本檔不驗 DB 內目前實際的 mapping_json（admin 可能在 Web UI 改過）。
- 不驗 _DIALECT_HEADER_MAPS（方言）；那是另一層機制。
- 不驗 fingerprint / variant_label 等 metadata；只看 mapping_json 的鍵值。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


# schema.sql 預期：INSERT INTO carrier_profiles ... $$\{...}$$::jsonb
# 用 PostgreSQL dollar-quoted string 抓出 JSON 內容。
_SEED_RE = re.compile(
    r"INSERT\s+INTO\s+carrier_profiles[\s\S]+?\$\$(\{[\s\S]+?\})\$\$::jsonb",
    re.IGNORECASE,
)


def _load_schema_seed_mapping() -> dict:
    """從 schema.sql 抽出 default profile seed 的 mapping_json。

    解析方式：regex 抓第一個 INSERT INTO carrier_profiles ... $$...$$::jsonb
    區塊。要找不到表示 schema 結構被改過 — 本檔需同步更新 regex。
    """
    schema_path = (
        Path(__file__).resolve().parents[2] / "app" / "db" / "schema.sql"
    )
    text = schema_path.read_text(encoding="utf-8")
    m = _SEED_RE.search(text)
    assert m, (
        "找不到 schema.sql 內 carrier_profiles 的 $$...$$ JSON seed —— "
        "schema 結構可能被改過，請更新本檔 regex"
    )
    return json.loads(m.group(1))


def test_raw2canon_and_seed_keys_match():
    """
    _RAW2CANON 與 schema.sql seed 的 key 集合必須完全一致。

    fail 時的處理流程：
      - code 有但 seed 缺 → 把缺的別名加進 schema.sql INSERT 的 JSON
        並更新 INSERT 末段的「N 個別名」註解。
      - seed 有但 code 缺 → 評估該別名是否仍需要：
        * 若需要 → 補進 ingest._RAW2CANON
        * 若已淘汰 → 從 schema.sql seed 移除
    """
    from app.services.ingest import _RAW2CANON
    seed = _load_schema_seed_mapping()

    code_keys = set(_RAW2CANON)
    seed_keys = set(seed)

    missing_in_db = code_keys - seed_keys
    extras_in_db = seed_keys - code_keys
    assert not missing_in_db, (
        f"_RAW2CANON 有但 schema.sql seed 缺：{sorted(missing_in_db)}\n"
        "請把這些別名加進 schema.sql 內 INSERT INTO carrier_profiles 的 "
        "$$...$$::jsonb JSON 區塊（保持同步避免 drift）。"
    )
    assert not extras_in_db, (
        f"schema.sql seed 有但 _RAW2CANON 缺：{sorted(extras_in_db)}\n"
        "請決定該別名要補回 _RAW2CANON 或從 seed 移除。"
    )


def test_raw2canon_and_seed_values_match():
    """
    同 key 對應的 canonical 必須一致（不能 code 寫 cell_id 而 seed 寫 cell_addr）。

    這條與 key match 拆開，是因為「值衝突」與「key 缺漏」需要的修法不同。
    """
    from app.services.ingest import _RAW2CANON
    seed = _load_schema_seed_mapping()

    conflicts = []
    for k in set(_RAW2CANON) & set(seed):
        if _RAW2CANON[k] != seed[k]:
            conflicts.append(f"  {k!r}: code={_RAW2CANON[k]!r} vs seed={seed[k]!r}")
    assert not conflicts, (
        "同 key 在 _RAW2CANON 與 schema.sql seed 有不同 canonical 值：\n"
        + "\n".join(conflicts)
        + "\n請評估真實正確值並同步兩處（W2.4 之後語意衝突應走 "
          "_DIALECT_HEADER_MAPS 而非雙向不一致）。"
    )


def test_seed_count_matches_raw2canon():
    """
    別名數量一致（key set match 已保證、本條是冗餘斷言但讓 fail 訊息一眼可懂）。

    註：schema.sql 內 INSERT 的 notes 欄位有寫「N 個別名」說明文，與本數字
    對齊更好（但 notes 是描述性、不在自動驗證範圍 — 加新欄位時順手改）。
    """
    from app.services.ingest import _RAW2CANON
    seed = _load_schema_seed_mapping()
    assert len(_RAW2CANON) == len(seed), (
        f"別名總數不一致：_RAW2CANON={len(_RAW2CANON)} vs seed={len(seed)}"
    )


def test_all_canonical_values_in_known_set():
    """
    canonical 值必須在已知 schema 欄位內 —— 防止 typo（例如 'cell_iD' 大小寫
    錯）讓 ingest 寫進 raw_traces 後悄悄失效。

    這條保護 _RAW2CANON / seed 兩端：任何 typo 都會被抓到。
    """
    from app.services.ingest import _RAW2CANON
    seed = _load_schema_seed_mapping()

    # raw_traces 對得上的 canonical key + W2.3 dispatch tag
    KNOWN = {
        "start_ts", "end_ts",
        "cell_id", "cell_addr", "sector_name", "site_code", "sector_id",
        "azimuth",
        "cell_id_compound",  # W2.3 dispatch tag（_normalize_row 拆解後分填）
    }
    for src, mapping in [("_RAW2CANON", _RAW2CANON), ("schema.sql seed", seed)]:
        bad = {k: v for k, v in mapping.items() if v not in KNOWN}
        assert not bad, (
            f"{src} 內有未知 canonical 值（疑似 typo）：{bad}\n"
            f"合法集合：{sorted(KNOWN)}"
        )
