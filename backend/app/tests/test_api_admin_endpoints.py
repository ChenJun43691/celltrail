# backend/app/tests/test_api_admin_endpoints.py
"""
管理端 API 的**行為層**測試（待辦 #2；DB-free，CI 可直接執行）。

背景 —— 為什麼這一批特別值得補：
  `test_api_p3p7.py` 只驗「路由有註冊」與「沒 token 回 401」，
  `test_cell_towers_import_cols.py` 驗的是離線 helper 函式，都碰不到 HTTP 層的行為。
  於是下列這些「壞掉會很慘、但壞了不會有人立刻發現」的守衛，至今零覆蓋：

  ① `DELETE /api/admin/cell-towers` 的 `?confirm=true` 護欄 —— 少了它，一次誤觸就
     清空整張基地台座標表。而那張表即將裝進數千筆座標，且**座標本身就是證據**（五-X）。
  ② `users` 的兩條自我鎖定守衛（不能降級自己、不能停用自己）—— 壞掉的話，
     最後一個 admin 可以把自己鎖在系統外，沒有任何補救路徑。
  ③ `account-requests` 的「非 pending 就 404」—— 少了它，重複核准同一筆申請會
     試圖建出第二個帳號。帳號建立是安全邊界，不能靠前端不重複點擊來保證。
  ④ 公開端點 `GET /api/account-requests/check-phone` 的回應內容 —— 它**無需驗證**，
     任何人都能打。必須確認它只回布林與狀態，不外洩姓名／帳號／單位。

沿用 test_share_links.py 的假 DB 手法；auth 以 dependency_overrides 注入。
"""
from __future__ import annotations

import io
import os
from contextlib import contextmanager

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:5500")
os.environ.setdefault("AUTH_ENABLED", "true")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_mod  # noqa: E402
from app.security import get_current_user, require_admin  # noqa: E402

app = main_mod.app
client = TestClient(app)

ADMIN = {"id": 1, "username": "admin1", "role": "admin"}
ADMIN2 = {"id": 2, "username": "admin2", "role": "admin"}
USER = {"id": 9, "username": "u9", "role": "user"}


# ── 假 DB：可腳本化多次查詢的結果，並記錄執行過的 SQL ──────────
class _FakeCursor:
    def __init__(self, script, log, rowcount):
        self._script = script          # list[fetchone 回傳值]；用完回 None
        self._log = log
        self._rowcount = rowcount      # 供 UPDATE ... WHERE status='pending' 這類「影響列數」分支
        self.rowcount = 0

    def __enter__(self): return self
    def __exit__(self, *exc): return False

    def execute(self, sql, params=None, *, prepare=None):
        self._log.append(" ".join(str(sql).split()))
        self.rowcount = self._rowcount

    def fetchone(self):
        return self._script.pop(0) if self._script else None

    def fetchall(self):
        return self._script.pop(0) if self._script else []


class _FakeConn:
    def __init__(self, script, log, rowcount):
        self._script, self._log, self._rowcount = script, log, rowcount

    def cursor(self): return _FakeCursor(self._script, self._log, self._rowcount)
    def commit(self): pass


def install_db(monkeypatch, module, script=None, rowcount=1):
    """把某個 api 模組的 get_conn 換成假連線；回傳「已執行 SQL」的 log。"""
    log: list[str] = []

    @contextmanager
    def fake_get_conn():
        yield _FakeConn(list(script or []), log, rowcount)

    monkeypatch.setattr(module, "get_conn", fake_get_conn)
    return log


def as_admin(user=ADMIN):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user


def as_user(user=USER):
    """非 admin：require_admin 依實作丟 403。"""
    from fastapi import HTTPException

    def _deny():
        raise HTTPException(status_code=403, detail="需要管理員權限")

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_admin] = _deny


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr("app.services.audit.write_audit", lambda **kw: 1, raising=False)
    for m in ("app.api.cell_towers", "app.api.users", "app.api.requests", "app.api.carrier_profile"):
        try:
            monkeypatch.setattr(f"{m}.write_audit", lambda **kw: 1)
        except AttributeError:
            pass
    yield
    app.dependency_overrides.clear()


def _sql_has(log, *needles):
    return any(all(n in s for n in needles) for s in log)


# ═══════════════════════════════════════════════════════════
# ① DELETE /api/admin/cell-towers —— 破壞性操作的確認護欄
# ═══════════════════════════════════════════════════════════
def test_clear_all_without_confirm_returns_400(monkeypatch):
    """沒帶 ?confirm=true 就要擋下。"""
    as_admin()
    install_db(monkeypatch, __import__("app.api.cell_towers", fromlist=["x"]))
    r = client.delete("/api/admin/cell-towers")
    assert r.status_code == 400, r.text
    assert "confirm" in r.text


def test_clear_all_without_confirm_executes_no_delete_sql(monkeypatch):
    """**這條才是重點**：不只回 400，而且絕不能已經把資料刪掉才回錯誤。

    座標表即將裝進數千筆資料，而基地台座標本身就是證據（五-X）。
    「先刪再抱怨」與「沒刪」在 HTTP 狀態碼上看起來一模一樣，故必須直接驗 SQL。
    """
    as_admin()
    import app.api.cell_towers as ct
    log = install_db(monkeypatch, ct)
    client.delete("/api/admin/cell-towers")
    assert not _sql_has(log, "DELETE", "cell_towers"), f"守衛前不得執行刪除；實際 SQL={log}"


def test_clear_all_with_confirm_deletes(monkeypatch):
    as_admin()
    import app.api.cell_towers as ct
    log = install_db(monkeypatch, ct, script=[(0,)])
    r = client.delete("/api/admin/cell-towers?confirm=true")
    assert r.status_code == 200, r.text
    assert _sql_has(log, "DELETE", "cell_towers")


def test_clear_all_requires_admin(monkeypatch):
    as_user()
    import app.api.cell_towers as ct
    log = install_db(monkeypatch, ct)
    r = client.delete("/api/admin/cell-towers?confirm=true")
    assert r.status_code == 403, r.text
    assert not _sql_has(log, "DELETE", "cell_towers"), "權限被拒時不得碰資料"


def test_clear_all_requires_token():
    app.dependency_overrides.clear()
    assert client.delete("/api/admin/cell-towers?confirm=true").status_code == 401


# ═══════════════════════════════════════════════════════════
# ② POST /api/admin/cell-towers/import —— HTTP 層的匯入行為
# ═══════════════════════════════════════════════════════════
def _import(csv_text: str):
    return client.post(
        "/api/admin/cell-towers/import",
        files={"file": ("towers.csv", csv_text.encode("utf-8"), "text/csv")},
    )


def test_import_counts_and_rejects_out_of_range(monkeypatch):
    """越界座標**逐列拒絕**、但不中斷整批 —— 一列髒資料不該讓整份對照表進不來。"""
    as_admin()
    import app.api.cell_towers as ct
    # 每筆成功 INSERT 的 RETURNING (was_insert,)
    install_db(monkeypatch, ct, script=[(True,), (True,)])
    r = _import("cell_id,lat,lng\nA1,22.6,120.3\nBAD,999,120.3\nA2,22.7,120.4\n")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["inserted"] == 2 and j["skipped"] == 1, j
    assert any("超出合理範圍" in e for e in j["errors"]), j["errors"]


def test_import_requires_admin(monkeypatch):
    as_user()
    import app.api.cell_towers as ct
    install_db(monkeypatch, ct)
    assert _import("cell_id,lat,lng\nA1,22.6,120.3\n").status_code == 403


# ═══════════════════════════════════════════════════════════
# ③ users —— 自我鎖定守衛（壞掉會把最後一個 admin 關在門外）
# ═══════════════════════════════════════════════════════════
def test_admin_cannot_demote_self(monkeypatch):
    as_admin(ADMIN)
    import app.api.users as U
    log = install_db(monkeypatch, U, script=[(1, "admin1", "user", None, None, None, None, True, False)])
    r = client.patch(f"/api/users/{ADMIN['id']}", json={"role": "user"})
    assert r.status_code == 400, r.text
    assert "降級自己" in r.text
    assert not _sql_has(log, "UPDATE users"), "守衛前不得寫入"


def test_admin_can_demote_someone_else(monkeypatch):
    """對照組：守衛只擋自己，不該把正常的降級也擋掉。"""
    as_admin(ADMIN)
    import app.api.users as U
    install_db(monkeypatch, U, script=[(9, "u9", "user", None, None, None, None, True, False)])
    r = client.patch("/api/users/9", json={"role": "user"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "user"


def test_admin_cannot_deactivate_self(monkeypatch):
    as_admin(ADMIN)
    import app.api.users as U
    log = install_db(monkeypatch, U, script=[(1, "admin1", "admin", None, None, None, None, False, False)])
    r = client.patch(f"/api/users/{ADMIN['id']}/deactivate")
    assert r.status_code == 400, r.text
    assert "停用自己" in r.text
    assert not _sql_has(log, "UPDATE users"), "守衛前不得寫入"


def test_admin_can_deactivate_someone_else(monkeypatch):
    as_admin(ADMIN)
    import app.api.users as U
    install_db(monkeypatch, U, script=[(9, "u9", "user", None, None, None, None, False, False)])
    assert client.patch("/api/users/9/deactivate").status_code == 200


def test_update_user_requires_at_least_one_field(monkeypatch):
    """空 PATCH 不該悄悄成功 —— 那會讓呼叫端以為改到了。"""
    as_admin()
    import app.api.users as U
    log = install_db(monkeypatch, U)
    r = client.patch("/api/users/9", json={})
    assert r.status_code == 400, r.text
    assert not _sql_has(log, "UPDATE users")


def test_update_user_missing_returns_404(monkeypatch):
    as_admin()
    import app.api.users as U
    install_db(monkeypatch, U, script=[None])       # RETURNING 無列
    assert client.patch("/api/users/999", json={"role": "user"}).status_code == 404


def test_user_endpoints_require_admin(monkeypatch):
    as_user()
    import app.api.users as U
    install_db(monkeypatch, U)
    assert client.get("/api/users").status_code == 403
    assert client.patch("/api/users/9", json={"role": "user"}).status_code == 403
    assert client.patch("/api/users/9/deactivate").status_code == 403


def test_reset_password_never_echoes_hash(monkeypatch):
    """重設密碼只回**臨時明文密碼**（僅此一次），絕不可回 hash。"""
    as_admin()
    import app.api.users as U
    install_db(monkeypatch, U, script=[(9, "u9", "user", None, None, None, None, True, True)])
    r = client.patch("/api/users/9", json={"reset_password": True})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j.get("temp_password"), "應回臨時密碼供管理員轉交"
    assert "password_hash" not in j and "hash" not in str(j).lower()


# ═══════════════════════════════════════════════════════════
# ④ account-requests —— 帳號建立是安全邊界
# ═══════════════════════════════════════════════════════════
def test_approve_non_pending_returns_404(monkeypatch):
    """重複核准必須擋下：少了它，同一筆申請會試圖建出第二個帳號。

    不能靠「前端不會重複點」來保證 —— 重送請求、按上一頁、網路重試都會發生。
    """
    as_admin()
    import app.api.requests as R
    log = install_db(monkeypatch, R, script=[None])   # 查不到 pending 申請
    r = client.post("/api/account-requests/5/approve")
    assert r.status_code == 404, r.text
    assert not _sql_has(log, "INSERT INTO users"), "非 pending 不得建帳號"


def test_approve_conflicting_username_returns_409(monkeypatch):
    """申請送出後、核准前，該帳號名被別人用掉 → 409，且不得覆蓋既有帳號。"""
    as_admin()
    import app.api.requests as R
    log = install_db(monkeypatch, R, script=[("dup", "王小明", "刑大", "hash"), (7,)])
    r = client.post("/api/account-requests/5/approve")
    assert r.status_code == 409, r.text
    assert not _sql_has(log, "INSERT INTO users")


def test_reject_non_pending_returns_404(monkeypatch):
    """reject 走的是 `UPDATE ... WHERE id=%s AND status='pending'` + 檢查 rowcount，
    與 approve 的先 SELECT 不同 —— 兩條路都要各自釘住，不能只測一邊。"""
    as_admin()
    import app.api.requests as R
    install_db(monkeypatch, R, rowcount=0)      # UPDATE 影響 0 列 = 非 pending
    r = client.post("/api/account-requests/5/reject", json={"reason": "資料不符"})
    assert r.status_code == 404, r.text


def test_reject_pending_succeeds(monkeypatch):
    as_admin()
    import app.api.requests as R
    install_db(monkeypatch, R, rowcount=1)
    r = client.post("/api/account-requests/5/reject", json={"reason": "資料不符"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"


def test_request_admin_endpoints_require_admin(monkeypatch):
    as_user()
    import app.api.requests as R
    log = install_db(monkeypatch, R)
    assert client.get("/api/account-requests").status_code == 403
    assert client.post("/api/account-requests/5/approve").status_code == 403
    assert client.post("/api/account-requests/5/reject",
                       json={"reason": "x"}).status_code == 403
    assert not _sql_has(log, "INSERT INTO users")


def test_submit_duplicate_username_returns_409(monkeypatch):
    as_admin()
    import app.api.requests as R
    # 查詢順序：① 電話重複 ② users 同名 ③ pending 申請同名
    install_db(monkeypatch, R, script=[None, (1,)])
    r = client.post("/api/account-requests", json={
        "username": "taken", "real_name": "王小明", "unit": "刑大",
        "phone": "0912345678", "password": "pw-12345678",
    })
    assert r.status_code == 409, r.text


# ── 公開端點：check-phone 不得外洩個資 ──────────────────────
def test_check_phone_is_public_and_leaks_nothing(monkeypatch):
    """這支**無需驗證**，任何人都能打。回應只能是布林與狀態字樣。

    若日後有人「順手」把姓名或帳號加進回應，這裡就變成一支
    以電話為索引的個資查詢介面 —— 而查詢對象正是刑案關係人。
    """
    app.dependency_overrides.clear()
    import app.api.requests as R
    install_db(monkeypatch, R, script=[("pending",)])
    r = client.get("/api/account-requests/check-phone?phone=0912345678")
    assert r.status_code == 200, r.text
    j = r.json()
    assert set(j) <= {"blocked", "status", "status_text"}, f"回應欄位超出預期：{j}"
    assert j["blocked"] is True


def test_check_phone_negative_case(monkeypatch):
    app.dependency_overrides.clear()
    import app.api.requests as R
    install_db(monkeypatch, R, script=[None])
    j = client.get("/api/account-requests/check-phone?phone=0900000000").json()
    assert j == {"blocked": False}


# ═══════════════════════════════════════════════════════════
# ⑤ carrier-profile —— 欄名對照表（改錯會讓整批檔案解析成 0 筆）
# ═══════════════════════════════════════════════════════════
@pytest.mark.parametrize("body", [
    {"raw_key": "", "canon_key": "start_ts"},
    {"raw_key": "始話時間", "canon_key": ""},
    {"raw_key": "  ", "canon_key": "  "},
])
def test_carrier_profile_rejects_empty_keys(monkeypatch, body):
    """空欄名要擋在寫入之前：寫進去會產生一條永遠命中不了的對應，
    而症狀會是「某業者的檔案突然解析不出東西」，很難回想到是這裡改壞的。"""
    as_admin()
    import app.api.carrier_profile as CP
    log = install_db(monkeypatch, CP)
    r = client.patch("/api/admin/carrier-profile/entry", json=body)
    assert r.status_code == 400, r.text
    assert not _sql_has(log, "UPDATE"), "驗證前不得寫入"


def test_carrier_profile_requires_admin(monkeypatch):
    as_user()
    import app.api.carrier_profile as CP
    install_db(monkeypatch, CP)
    assert client.get("/api/admin/carrier-profile").status_code == 403
    assert client.patch("/api/admin/carrier-profile/entry",
                        json={"raw_key": "a", "canon_key": "start_ts"}).status_code == 403


# ═══════════════════════════════════════════════════════════
# ⑥ audit —— 稽核軌跡本身就是證據，讀取權限與參數化查詢都要守住
# ═══════════════════════════════════════════════════════════
def test_audit_logs_requires_admin(monkeypatch):
    """稽核日誌記載「誰在何時對哪個案件做了什麼」，含 IP 與 user agent。
    它同時是證據鏈的一部分，也是跨案件的行為紀錄 —— 一般使用者不得讀取。"""
    as_user()
    import app.api.audit as A
    log = install_db(monkeypatch, A)
    assert client.get("/api/audit/logs").status_code == 403
    assert not log, "權限被拒時不得查詢資料庫"


def test_audit_logs_admin_ok(monkeypatch):
    as_admin()
    import app.api.audit as A
    install_db(monkeypatch, A, script=[(0,), []])   # COUNT(*) 然後列表
    r = client.get("/api/audit/logs")
    assert r.status_code == 200, r.text
    j = r.json()
    assert {"page", "page_size", "total", "items"} <= set(j)


def test_audit_logs_filters_are_parameterised(monkeypatch):
    """篩選條件必須走參數化（`= %s`），不得把使用者輸入拼進 SQL 字串。

    這裡直接檢查產生的 SQL：若日後有人為了「方便」改成 f-string 拼接，
    這條會紅。稽核表是唯一能回答「這筆證據怎麼來的」的地方，被注入等於失去可信度。
    """
    as_admin()
    import app.api.audit as A
    log = install_db(monkeypatch, A, script=[(0,), []])
    evil = "x'; DROP TABLE audit_logs; --"
    r = client.get("/api/audit/logs", params={"project_id": evil, "action": "upload"})
    assert r.status_code == 200, r.text
    joined = " ".join(log)
    assert "DROP TABLE" not in joined, f"使用者輸入被拼進 SQL：{joined}"
    assert "project_id = %s" in joined and "action = %s" in joined


def test_audit_logs_page_size_is_capped(monkeypatch):
    """page_size 有上限（le=1000）：沒有上限的話一次查詢就能把整張稽核表拉出來。"""
    as_admin()
    import app.api.audit as A
    install_db(monkeypatch, A, script=[(0,), []])
    assert client.get("/api/audit/logs", params={"page_size": 5000}).status_code == 422
    assert client.get("/api/audit/logs", params={"page": 0}).status_code == 422


def test_audit_actions_available_to_any_logged_in_user(monkeypatch):
    """對照組：action 清單只是列舉可篩選的動作名稱，不含任何案件內容，
    故刻意開放給一般登入者（前端 audit.html 的下拉選單要用）。
    這條同時守住「不要哪天順手把它一起鎖成 admin」。"""
    as_user()
    import app.api.audit as A
    install_db(monkeypatch, A, script=[[]])
    assert client.get("/api/audit/actions").status_code == 200


def test_audit_endpoints_reject_anonymous():
    app.dependency_overrides.clear()
    assert client.get("/api/audit/logs").status_code == 401
    assert client.get("/api/audit/actions").status_code == 401
