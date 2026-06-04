"""
GPS 軌跡 / 經緯度直給格式解析測試（DB-free）。

背景（2026-06-04）：
- 新增「檔案自帶經緯度」格式支援（如 RFL-8271軌跡.xlsx：車號/GPS時間/經度/緯度），
  此類檔無 cell_id/地址，ingest 直接採用座標、免 geocode。
- 兩個易回歸的核心：
  ① _resolve_latlng 的「範圍自動校正」：實務上經緯度欄常被標反（值對調），
     以「緯度必落在 [-90,90]」自動對調，避免把點畫到海裡。
  ② _parse_ts 支援 GPS 車機的 M/D/YYYY 12 小時 AM/PM 時間格式。
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")

import pytest

from app.services.ingest import (
    _resolve_latlng, _parse_ts, _RAW2CANON,
    _reject_if_encrypted, EncryptedFileError, _OLE2_MAGIC,
)


# ── _resolve_latlng：範圍自動校正 ──────────────────────────────
def test_latlng_normal_order():
    """標頭正確（lat=22.6, lng=120.3）→ 原樣回傳。"""
    assert _resolve_latlng({"lat": "22.6", "lng": "120.3"}) == (22.6, 120.3)


def test_latlng_swapped_columns_auto_corrected():
    """
    標反（lat 欄裝 120.3、lng 欄裝 22.6）—— RFL-8271軌跡.xlsx 的真實情況。
    120.3 > 90 不可能是緯度 → 自動對調為 (22.6, 120.3)。
    """
    assert _resolve_latlng({"lat": "120.3285400", "lng": "22.5964200"}) == (22.59642, 120.32854)


def test_latlng_missing_returns_none():
    """缺任一座標 → None（讓該列走 cell_id/addr geocode 路徑）。"""
    assert _resolve_latlng({"lat": "22.6"}) is None
    assert _resolve_latlng({"lat": None, "lng": "120.3"}) is None
    assert _resolve_latlng({}) is None


def test_latlng_zero_zero_rejected():
    """(0,0) 視為無效座標（常見的空值佔位）→ None。"""
    assert _resolve_latlng({"lat": "0", "lng": "0"}) is None


def test_latlng_both_out_of_range_rejected():
    """兩者皆超界（無從判斷）→ None，不亂猜。"""
    assert _resolve_latlng({"lat": "200", "lng": "300"}) is None


def test_latlng_non_numeric_returns_none():
    assert _resolve_latlng({"lat": "abc", "lng": "120.3"}) is None


# ── _parse_ts：GPS 車機 AM/PM 時間 ─────────────────────────────
def test_parse_ts_gps_ampm_format():
    """'4/18/2026 3:51:09 AM' → 2026-04-18 03:51:09（+08）。"""
    dt = _parse_ts("4/18/2026 3:51:09 AM")
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) == (2026, 4, 18, 3, 51, 9)


def test_parse_ts_gps_ampm_pm_noon_boundary():
    """PM 應正確 +12（下午 3 點 → 15 時）。"""
    dt = _parse_ts("12/31/2025 3:05:00 PM")
    assert dt is not None
    assert (dt.month, dt.day, dt.hour, dt.minute) == (12, 31, 15, 5)


def test_parse_ts_existing_formats_still_work():
    """既有格式不受新增 AM/PM 影響（回歸保護）。"""
    assert _parse_ts("2024-09-01 20:06:44") is not None
    assert _parse_ts("2023-01-12T00:48:02.000") is not None


# ── 欄名對照：經緯度別名已登記 ─────────────────────────────────
def test_latlng_aliases_registered():
    """經度→lng、緯度→lat、GPS時間→start_ts 等已進 _RAW2CANON。"""
    assert _RAW2CANON["經度"] == "lng"
    assert _RAW2CANON["緯度"] == "lat"
    assert _RAW2CANON["GPS時間"] == "start_ts"
    assert _RAW2CANON["latitude"] == "lat"
    assert _RAW2CANON["longitude"] == "lng"


# ── 加密 / 密碼保護檔偵測（不解密，只報錯提醒）─────────────────
def test_encrypted_file_rejected():
    """OLE2/CDFV2 檔頭（密碼保護 xlsx）→ 拋 EncryptedFileError。"""
    fake_encrypted = _OLE2_MAGIC + b"\x00" * 512
    with pytest.raises(EncryptedFileError):
        _reject_if_encrypted(fake_encrypted)


def test_normal_xlsx_zip_not_rejected():
    """一般 xlsx（zip，PK\\x03\\x04 開頭）→ 不報錯。"""
    fake_zip = b"PK\x03\x04" + b"\x00" * 64
    _reject_if_encrypted(fake_zip)  # 不應拋例外


def test_csv_bytes_not_rejected():
    """CSV 純文字 → 不報錯。"""
    _reject_if_encrypted("時間,基地台\n2026-01-01,001\n".encode("utf-8"))
