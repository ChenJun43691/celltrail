# backend/app/tests/test_azimuth_ref.py
"""
方位角基準（azimuth_ref）相關單元測試 —— 不依賴 DB。

純函式覆蓋：
  - UpdateAzimuthRefIn Pydantic 模型：
      * ref 必須是 'magnetic' / 'true' / 'unknown' 三選一（白名單）
      * evidence 至少 5 字元（非空且非過短）
      * 兩者皆必填（field required）

為什麼要測這層：
  Pydantic Literal + Field min_length 是 framework 行為，但這裡的「白名單」
  與「最小長度 5」是法庭可防禦性要件 ── 任何人若把 Literal 改寬鬆、
  或拿掉 min_length，整個 audit chain 的書面依據完整性就會崩。
  把這個契約鎖在 CI 上，避免「好意改寬鬆」的退化。
"""
from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

# 必要環境變數注入（避免 import 鏈內 db.session 等 import 時崩潰）
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


# ------------------------------------------------------------------
# 合法值
# ------------------------------------------------------------------
def test_accepts_magnetic_with_evidence():
    from app.api.targets import UpdateAzimuthRefIn

    m = UpdateAzimuthRefIn(
        ref="magnetic",
        evidence="中華電信 2026-01-15 函覆 字第 1150000123 號",
    )
    assert m.ref == "magnetic"
    assert "中華電信" in m.evidence


def test_accepts_true_with_evidence():
    from app.api.targets import UpdateAzimuthRefIn

    m = UpdateAzimuthRefIn(
        ref="true",
        evidence="台灣大哥大規格書 v3.2 第 14 頁明定",
    )
    assert m.ref == "true"


def test_accepts_unknown_with_evidence():
    """即使是 unknown 也要寫 evidence（例如：保守原則維持）"""
    from app.api.targets import UpdateAzimuthRefIn

    m = UpdateAzimuthRefIn(
        ref="unknown",
        evidence="電信業者拒絕回覆，依保守原則維持 unknown",
    )
    assert m.ref == "unknown"


# ------------------------------------------------------------------
# 非法值 ── 白名單外
# ------------------------------------------------------------------
def test_rejects_invalid_ref_value():
    """任何不在白名單內的值（例如手滑打錯字）都應 reject"""
    from app.api.targets import UpdateAzimuthRefIn

    for bad in ("MAG", "Magnetic", "north", "true_north", "", "None", "null"):
        with pytest.raises(ValidationError):
            UpdateAzimuthRefIn(ref=bad, evidence="some valid evidence")


def test_rejects_evidence_too_short():
    """evidence < 5 字（含 0 字元）都應 reject —— 法庭可防禦性最低門檻"""
    from app.api.targets import UpdateAzimuthRefIn

    for bad in ("", "x", "abcd"):  # 0/1/4 字元
        with pytest.raises(ValidationError):
            UpdateAzimuthRefIn(ref="magnetic", evidence=bad)


def test_accepts_evidence_exactly_5_chars():
    """min_length 是 inclusive：剛好 5 字元應通過"""
    from app.api.targets import UpdateAzimuthRefIn

    m = UpdateAzimuthRefIn(ref="unknown", evidence="abcde")
    assert m.evidence == "abcde"


# ------------------------------------------------------------------
# 缺欄位
# ------------------------------------------------------------------
def test_rejects_missing_ref():
    from app.api.targets import UpdateAzimuthRefIn

    with pytest.raises(ValidationError):
        UpdateAzimuthRefIn(evidence="一些書面依據說明")  # 缺 ref


def test_rejects_missing_evidence():
    from app.api.targets import UpdateAzimuthRefIn

    with pytest.raises(ValidationError):
        UpdateAzimuthRefIn(ref="magnetic")  # 缺 evidence


# ------------------------------------------------------------------
# 中文 evidence（UTF-8 多 byte）
# ------------------------------------------------------------------
def test_accepts_chinese_evidence():
    """min_length 在 Pydantic 是『字元數』而非 byte 數，中文 5 字應通過"""
    from app.api.targets import UpdateAzimuthRefIn

    m = UpdateAzimuthRefIn(
        ref="magnetic",
        evidence="中文五字依據",  # 5 個中文字 = 15 byte，但字元數 5
    )
    assert len(m.evidence) >= 5
