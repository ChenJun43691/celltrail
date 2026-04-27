# backend/app/tests/test_evidence.py
"""
Evidence service 單元測試 —— 鎖死 byte-for-byte SHA-256 行為。

為什麼要這個測試：
  evidence.sha256_full() 是「整個證物指紋鏈的根」。
  若哪天有人手滑把它改成 sha1 / md5、或是把 .hexdigest() 改成 .digest()（變 bytes），
  整套法庭採信邏輯立刻崩潰。這個測試把「正確行為」鎖在 CI 上。

純函式覆蓋（不依賴 DB）：
  - sha256_full：對已知 byte 序列產生已知 SHA-256
  - 空 bytes 的 hash 為 NIST 公開常數（不可變的 sanity check）
  - 大小寫敏感（abc != ABC）
  - 1 byte 變動就應產生完全不同 hash（avalanche 特性）
  - 回傳一定是 64 hex 字元的 str（不是 bytes、不是 hash object）
"""
from __future__ import annotations

import hashlib
import os

# 注入必要環境變數（防止 import 鏈內 db.session 等 import 時崩潰）
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")


# ------------------------------------------------------------------
# sha256_full：已知向量
# ------------------------------------------------------------------
def test_sha256_full_empty_bytes_known_constant():
    """
    SHA-256("") 的 hex digest 是 NIST 公開常數，永不會變。
    若這個 assertion 跳掉，代表函式不再走 SHA-256（嚴重錯誤）。
    """
    from app.services.evidence import sha256_full

    # NIST FIPS 180-4: SHA-256 of empty input
    expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_full(b"") == expected


def test_sha256_full_known_vector_abc():
    """
    SHA-256("abc") 也是 NIST 標準測試向量，固定值。
    """
    from app.services.evidence import sha256_full

    # NIST FIPS 180-2 Appendix B test vector
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert sha256_full(b"abc") == expected


def test_sha256_full_returns_lowercase_hex_64():
    """
    回傳必須是 64 字元小寫 hex（這是 audit_logs / evidence_files
    schema 寫入欄位長度的隱式契約）。
    """
    from app.services.evidence import sha256_full

    h = sha256_full(b"some arbitrary content for testing 123 \xe4\xb8\xad\xe6\x96\x87")
    assert isinstance(h, str)
    assert len(h) == 64
    # 不能含大寫（hexdigest 預設小寫；如改成 .upper() 會破壞跨系統比對）
    assert h == h.lower()
    # 必須只含 0-9 / a-f
    assert all(c in "0123456789abcdef" for c in h)


def test_sha256_full_case_sensitive():
    """
    'abc' 和 'ABC' 是不同 byte 序列，hash 必定不同（防止有人加 .lower() 預處理）。
    """
    from app.services.evidence import sha256_full

    assert sha256_full(b"abc") != sha256_full(b"ABC")


def test_sha256_full_avalanche_one_bit_change():
    """
    Avalanche 特性：1 byte 變動 → hash 應差異極大（隨便檢查不為相同即可）。
    這個測試攔截「函式回傳常數」之類的退化錯誤。
    """
    from app.services.evidence import sha256_full

    a = b"The quick brown fox jumps over the lazy dog"
    b = b"The quick brown fox jumps over the lazy dog!"  # 多一個 byte
    ha = sha256_full(a)
    hb = sha256_full(b)
    assert ha != hb
    # 兩個 hex 串應幾乎沒有相同位置（不檢精確比例，留容錯）
    same_positions = sum(1 for x, y in zip(ha, hb) if x == y)
    assert same_positions < 32  # 64 字元中相同位數 < 50%（隨機期望 ~4 個）


def test_sha256_full_chinese_utf8_bytes():
    """
    中文證物（檔名、CSV 內容）一定走 UTF-8 byte。
    驗證對 UTF-8 編碼 byte 算出來的值，與 hashlib 直算一致（防自製實作走偏）。
    """
    from app.services.evidence import sha256_full

    payload = "高雄市苓雅區三多四路117號".encode("utf-8")
    expected = hashlib.sha256(payload).hexdigest()
    assert sha256_full(payload) == expected


def test_sha256_full_large_input_consistent():
    """
    大檔（1 MB）也應與 hashlib 直算一致 —— 確保函式沒做 chunk 截斷。
    """
    from app.services.evidence import sha256_full

    blob = b"\x00\xff\x42" * 350_000  # ~1.05 MB
    expected = hashlib.sha256(blob).hexdigest()
    assert sha256_full(blob) == expected
