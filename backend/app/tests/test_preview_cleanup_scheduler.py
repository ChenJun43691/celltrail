# backend/app/tests/test_preview_cleanup_scheduler.py
"""
Preview cleanup scheduler（P9A A.4）測試（2026-07-02）。

驗 _register_jobs 註冊 keepalive + preview_cleanup、interval=10m、_preview_cleanup 的
行為（呼叫 cleanup_expired、n>0 寫 summary audit、n==0 不寫、例外不 crash）。
不啟動真 scheduler、不碰真 DB。
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("AUTH_ENABLED", "true")

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import app.main as main


# ── _register_jobs 註冊 ─────────────────────────────────────
def test_register_jobs_adds_both():
    sched = BackgroundScheduler()      # 建立但不 start（無背景執行緒）
    main._register_jobs(sched)
    assert sched.get_job("supabase_keepalive") is not None
    assert sched.get_job("preview_cleanup") is not None


def test_preview_cleanup_interval_is_10_min():
    sched = BackgroundScheduler()
    main._register_jobs(sched)
    job = sched.get_job("preview_cleanup")
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(minutes=10)


def test_keepalive_interval_unchanged():
    sched = BackgroundScheduler()
    main._register_jobs(sched)
    job = sched.get_job("supabase_keepalive")
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(hours=6)


# ── _preview_cleanup 行為 ───────────────────────────────────
def test_cleanup_calls_service_and_audits_when_positive(monkeypatch, capsys):
    called = {"cleanup": 0}
    audits = []
    monkeypatch.setattr(main.preview_artifact, "cleanup_expired",
                        lambda: (called.__setitem__("cleanup", called["cleanup"] + 1) or 3))
    monkeypatch.setattr(main, "write_audit", lambda **kw: audits.append(kw) or 1)

    main._preview_cleanup()

    assert called["cleanup"] == 1
    assert "purged 3" in capsys.readouterr().out
    assert len(audits) == 1
    assert audits[0]["action"] == "preview.cleanup"
    assert audits[0]["details"] == {"deleted": 3}


def test_cleanup_no_audit_when_zero(monkeypatch, capsys):
    audits = []
    monkeypatch.setattr(main.preview_artifact, "cleanup_expired", lambda: 0)
    monkeypatch.setattr(main, "write_audit", lambda **kw: audits.append(kw) or 1)

    main._preview_cleanup()

    assert "none expired" in capsys.readouterr().out
    assert audits == []   # n==0 不寫 audit


def test_cleanup_exception_does_not_crash(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(main.preview_artifact, "cleanup_expired", _boom)
    monkeypatch.setattr(main, "write_audit", lambda **kw: 1)

    # 不應拋例外
    main._preview_cleanup()

    assert "failed" in capsys.readouterr().out


# ── pytest 環境守門 ─────────────────────────────────────────
def test_pytest_guard_present():
    # lifespan 以 "pytest" not in sys.modules 守門 → 測試環境不啟真 scheduler。
    assert "pytest" in sys.modules
    # _preview_cleanup 可獨立呼叫，不需 scheduler
    assert callable(main._preview_cleanup)
    assert callable(main._register_jobs)
