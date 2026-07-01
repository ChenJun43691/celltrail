# backend/app/services/crypto_box.py
"""
Preview artifact 靜態加密層（P9A A.1，2026-07-01）。

用途：把 preview 的「原始檔 bytes」在存入 preview_artifacts 前先 **gzip → AES-256-GCM**
加密，作為 defense-in-depth——即使 DB dump 外洩，短命的 preview 原始通聯檔（含門號 /
IMEI / 基地台 / 通聯 PII）也不是明文。

設計（皆為本輪鎖定決策）：
  - 依賴：cryptography（已釘 requirements.txt ==46.0.1，A.1 不新增依賴）。
  - 金鑰：env `PREVIEW_ARTIFACT_KEY` = **hex 64 字元 = 32 bytes**（AES-256）。
          `openssl rand -hex 32` 產生。缺失 / 格式錯 → raise PreviewKeyError（fail-closed，
          絕不 fallback 明文儲存）。
  - blob layout 固定：**[version:1][nonce:12][ciphertext+tag:n]**
      * version = ENC_VERSION（目前 1）；預留未來金鑰 / 演算法輪替。
      * nonce = os.urandom(12)（AES-GCM 標準 96-bit nonce；每次隨機，不重用）。
      * ciphertext+tag = AESGCM.encrypt(...) 的輸出（cryptography 已把 16-byte GCM tag
        併在 ciphertext 尾端）。
  - 只做 bytes↔bytes；不碰 DB / API / 檔案系統（純函式，好測、零副作用）。
"""
from __future__ import annotations

import gzip
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["ENC_VERSION", "ENC_ALG", "PreviewKeyError", "encrypt_blob", "decrypt_blob"]

# ── blob 格式常數 ──────────────────────────────────────────────
ENC_VERSION = 1          # blob 第 0 個 byte；改演算法 / 金鑰派生時 +1
ENC_ALG = "aesgcm-v1"    # 給 preview_artifacts.enc_alg 欄位記錄用（人類可讀）
_NONCE_LEN = 12          # AES-GCM 96-bit nonce
_GCM_TAG_LEN = 16        # AES-GCM 128-bit tag（cryptography 併在 ciphertext 尾端）
_KEY_ENV = "PREVIEW_ARTIFACT_KEY"
_KEY_LEN = 32            # AES-256


class PreviewKeyError(RuntimeError):
    """金鑰缺失或格式錯誤（fail-closed；API 層應轉 503，絕不以明文儲存）。"""


def _load_key() -> bytes:
    """從 env 讀取並驗證 32-byte 金鑰；任何問題一律 raise PreviewKeyError（fail-closed）。"""
    raw = os.getenv(_KEY_ENV)
    if not raw or not raw.strip():
        raise PreviewKeyError(
            f"{_KEY_ENV} 未設定 —— fail-closed，拒絕以明文儲存 preview 原始檔。"
            f"請以 `openssl rand -hex 32` 產生 64 字元 hex 金鑰。"
        )
    s = raw.strip()
    try:
        key = bytes.fromhex(s)
    except ValueError as e:
        raise PreviewKeyError(f"{_KEY_ENV} 非合法 hex（需 64 個 hex 字元）：{e}") from e
    if len(key) != _KEY_LEN:
        raise PreviewKeyError(
            f"{_KEY_ENV} 須為 {_KEY_LEN} bytes（{_KEY_LEN * 2} 個 hex 字元），實際 {len(key)} bytes。"
        )
    return key


def encrypt_blob(raw: bytes) -> bytes:
    """gzip(raw) → AES-256-GCM → 回 [version:1][nonce:12][ciphertext+tag:n]。

    金鑰缺失 / 格式錯 → PreviewKeyError（fail-closed）。
    """
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise TypeError(f"encrypt_blob 只接受 bytes，收到 {type(raw).__name__}")
    key = _load_key()
    gz = gzip.compress(bytes(raw))
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, gz, None)   # ct 已含 16-byte GCM tag
    return bytes([ENC_VERSION]) + nonce + ct


def decrypt_blob(blob: bytes) -> bytes:
    """反向：解出原始 raw bytes。

    - version 不符 → ValueError
    - 被竄改 / 金鑰不符（GCM 驗章失敗）→ ValueError
    - 長度不足 → ValueError
    - 金鑰缺失 / 格式錯 → PreviewKeyError
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise TypeError(f"decrypt_blob 只接受 bytes，收到 {type(blob).__name__}")
    blob = bytes(blob)
    if len(blob) < 1 + _NONCE_LEN + _GCM_TAG_LEN:
        raise ValueError("blob 過短，非合法 preview 加密格式")
    ver = blob[0]
    if ver != ENC_VERSION:
        raise ValueError(f"不支援的加密版本：{ver}（預期 {ENC_VERSION}）")
    key = _load_key()
    nonce = blob[1:1 + _NONCE_LEN]
    ct = blob[1 + _NONCE_LEN:]
    try:
        gz = AESGCM(key).decrypt(nonce, ct, None)
    except InvalidTag as e:
        raise ValueError("解密失敗：GCM 驗章不符（內容被竄改或金鑰錯誤）") from e
    return gzip.decompress(gz)
