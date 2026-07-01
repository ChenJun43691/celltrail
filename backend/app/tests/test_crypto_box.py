# backend/app/tests/test_crypto_box.py
"""
crypto_box（Preview artifact 靜態加密層）測試（P9A A.1，2026-07-01）。

覆蓋：roundtrip（binary / 中文 / 空）、blob layout、nonce 隨機、竄改偵測、
版本不符、fail-closed（缺金鑰 / 非法 hex / 長度錯）。

不碰 DB / API；金鑰以 monkeypatch.setenv 提供，不依賴真實環境變數。
"""
from __future__ import annotations

import os

import pytest

from app.services.crypto_box import (
    ENC_VERSION,
    ENC_ALG,
    PreviewKeyError,
    encrypt_blob,
    decrypt_blob,
)

# 64 個 hex 字元 = 32 bytes（AES-256）
_VALID_KEY = "a1b2c3d4" * 8


@pytest.fixture
def key(monkeypatch):
    """提供合法 32-byte 金鑰。"""
    monkeypatch.setenv("PREVIEW_ARTIFACT_KEY", _VALID_KEY)
    return _VALID_KEY


# ── roundtrip ────────────────────────────────────────────────
def test_roundtrip_binary(key):
    raw = os.urandom(1024)
    assert decrypt_blob(encrypt_blob(raw)) == raw


def test_roundtrip_chinese_utf8(key):
    raw = "高雄市前金區中正四路211號｜門號0912345678".encode("utf-8")
    assert decrypt_blob(encrypt_blob(raw)) == raw


def test_roundtrip_empty_bytes(key):
    assert decrypt_blob(encrypt_blob(b"")) == b""


def test_roundtrip_large(key):
    raw = os.urandom(2 * 1024 * 1024)  # 2MB（DB 分支上限內）
    assert decrypt_blob(encrypt_blob(raw)) == raw


# ── blob layout ──────────────────────────────────────────────
def test_blob_layout(key):
    blob = encrypt_blob(b"hello world")
    assert blob[0] == ENC_VERSION == 1          # version = 1
    assert len(blob[1:13]) == 12                # nonce length = 12
    # version(1) + nonce(12) + gzip(至少含 header) + GCM tag(16) → 合理下限
    assert len(blob) >= 1 + 12 + 16


def test_enc_alg_constant():
    assert ENC_ALG == "aesgcm-v1"


# ── nonce 隨機：同明文兩次加密結果不同 ───────────────────────
def test_same_plaintext_different_blob(key):
    raw = b"same plaintext"
    b1 = encrypt_blob(raw)
    b2 = encrypt_blob(raw)
    assert b1 != b2                              # nonce 隨機 → 不同 blob
    assert decrypt_blob(b1) == decrypt_blob(b2) == raw


# ── 竄改偵測 ─────────────────────────────────────────────────
def test_tampered_ciphertext_fails(key):
    blob = bytearray(encrypt_blob(b"authentic payload"))
    blob[-1] ^= 0x01                            # 翻轉 GCM tag 尾端一個 bit
    with pytest.raises(ValueError):
        decrypt_blob(bytes(blob))


def test_tampered_nonce_fails(key):
    blob = bytearray(encrypt_blob(b"authentic payload"))
    blob[5] ^= 0xFF                             # 動 nonce 區段
    with pytest.raises(ValueError):
        decrypt_blob(bytes(blob))


def test_wrong_version_fails(key):
    blob = bytearray(encrypt_blob(b"payload"))
    blob[0] = 9                                 # 非 ENC_VERSION
    with pytest.raises(ValueError):
        decrypt_blob(bytes(blob))


def test_too_short_blob_fails(key):
    with pytest.raises(ValueError):
        decrypt_blob(bytes([ENC_VERSION]) + b"\x00" * 5)


# ── fail-closed：金鑰問題 ────────────────────────────────────
def test_missing_key_fails_closed(monkeypatch):
    monkeypatch.delenv("PREVIEW_ARTIFACT_KEY", raising=False)
    with pytest.raises(PreviewKeyError):
        encrypt_blob(b"x")
    with pytest.raises(PreviewKeyError):
        decrypt_blob(bytes([ENC_VERSION]) + b"\x00" * 40)


def test_empty_key_fails_closed(monkeypatch):
    monkeypatch.setenv("PREVIEW_ARTIFACT_KEY", "   ")
    with pytest.raises(PreviewKeyError):
        encrypt_blob(b"x")


def test_invalid_hex_key_fails(monkeypatch):
    monkeypatch.setenv("PREVIEW_ARTIFACT_KEY", "zz" * 32)  # 64 字元但非 hex
    with pytest.raises(PreviewKeyError):
        encrypt_blob(b"x")


def test_wrong_length_key_fails(monkeypatch):
    monkeypatch.setenv("PREVIEW_ARTIFACT_KEY", "ab" * 16)  # 32 hex 字元 = 16 bytes
    with pytest.raises(PreviewKeyError):
        encrypt_blob(b"x")


# ── 型別守衛 ─────────────────────────────────────────────────
def test_encrypt_rejects_non_bytes(key):
    with pytest.raises(TypeError):
        encrypt_blob("我是字串不是bytes")  # type: ignore[arg-type]
