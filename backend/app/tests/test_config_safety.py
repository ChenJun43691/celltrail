"""
啟動設定安全自檢 fail-fast 測試（DB-free）。

背景（2026-06-13 資安檢視）：
- 雲端 Render 設的環境變數是 JWT_SECRET，但程式只讀 SECRET_KEY → fallback 到
  程式碼裡公開的預設值 "change-me-please" → 任何人可用該預設值偽造 admin token
  （實測雲端 /auth/me 用偽造 token 回 200 admin）。
- 修法：security.SECRET_KEY 同時讀 SECRET_KEY / JWT_SECRET；main._config_safety_audit
  在 AUTH_ENABLED（疑似正式環境）且金鑰仍是預設值時 fail-fast（raise）拒絕啟動，
  把「只警告」升級為「不准用可偽造密鑰對外服務」。
"""
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5500")
os.environ.setdefault("AUTH_ENABLED", "true")


def test_fail_fast_on_default_secret_in_production(monkeypatch):
    """AUTH_ENABLED=true 且金鑰是公開預設值 → 拒絕啟動（raise）。"""
    import app.main as main
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "SECRET_KEY", "change-me-please")
    with pytest.raises(RuntimeError):
        main._config_safety_audit()


def test_fail_fast_on_empty_secret_in_production(monkeypatch):
    """空字串金鑰同樣拒絕。"""
    import app.main as main
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "SECRET_KEY", "")
    with pytest.raises(RuntimeError):
        main._config_safety_audit()


def test_default_secret_in_dev_only_warns(monkeypatch):
    """AUTH_ENABLED=false（本機開發）→ 預設金鑰只警告、不擋啟動。"""
    import app.main as main
    monkeypatch.setattr(main, "AUTH_ENABLED", False)
    monkeypatch.setattr(main, "SECRET_KEY", "change-me-please")
    main._config_safety_audit()  # 不應 raise


def test_strong_secret_passes(monkeypatch):
    """production + 強隨機金鑰 → 通過。"""
    import app.main as main
    monkeypatch.setattr(main, "AUTH_ENABLED", True)
    monkeypatch.setattr(main, "SECRET_KEY", "a" * 64)
    main._config_safety_audit()  # 不應 raise


def test_security_reads_jwt_secret_alias(monkeypatch):
    """security 模組應同時接受 JWT_SECRET（雲端慣用名），不再 fallback 到預設。"""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("JWT_SECRET", "from-jwt-secret-env-strong-value-0123456789")
    import importlib
    import app.security as sec
    importlib.reload(sec)
    try:
        assert sec.SECRET_KEY == "from-jwt-secret-env-strong-value-0123456789"
    finally:
        # 還原模組狀態，避免污染其他測試
        monkeypatch.setenv("SECRET_KEY", "test-secret-key-only-for-pytest")
        importlib.reload(sec)
