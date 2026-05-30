"""
證物報告地圖截圖尺寸計算 _fit_image_dims 測試（DB-free）。

背景（2026-05-30 修）：
- evidence-report 端點曾在「高瘦的地圖範圍（直式 bbox）」時回 500：
  reportlab LayoutError —— 地圖截圖只鎖了寬度（170mm），高度任由 aspect
  ratio 放大到 963pt，超過 A4 頁框可用高度（約 728pt），整份報告產不出來。
- 修法：尺寸計算改為「同時受限於 max_w 與 max_h」，抽成純函式
  _fit_image_dims 以利回歸守護。
- 本檔釘住該數學：高瘦圖必須被縮到不超過 max_h，且任何情況下寬高都不超界、
  aspect ratio 不變。
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/fakedb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key-only-for-pytest")

from app.services.report import _fit_image_dims

MAX_W, MAX_H = 168 * 2.834645669, 200 * 2.834645669  # mm→pt（與 report.py 同口徑）


def _within_bounds(w, h):
    # 容許極小浮點誤差
    return w <= MAX_W + 1e-6 and h <= MAX_H + 1e-6


def test_tall_portrait_image_is_capped_by_height():
    """
    高瘦圖（h_px 遠大於 w_px）—— 這正是 500 的觸發情境：
    高度必須被 max_h 卡住，寬度隨之縮小，兩者皆不超界。
    """
    w, h = _fit_image_dims(400, 900, MAX_W, MAX_H)
    assert _within_bounds(w, h)
    # 高度應貼齊 max_h（受高度限制），而非被寬度撐爆
    assert abs(h - MAX_H) < 1e-6
    assert w < MAX_W  # 寬度必然小於上限


def test_wide_landscape_image_is_capped_by_width():
    """寬扁圖 —— 受寬度限制：寬度貼齊 max_w，高度遠小於 max_h。"""
    w, h = _fit_image_dims(1600, 400, MAX_W, MAX_H)
    assert _within_bounds(w, h)
    assert abs(w - MAX_W) < 1e-6
    assert h < MAX_H


def test_aspect_ratio_preserved():
    """不論受哪一邊限制，輸出長寬比必須等於原圖長寬比（不變形）。"""
    for w_px, h_px in [(400, 900), (1600, 400), (760, 760), (1024, 300)]:
        w, h = _fit_image_dims(w_px, h_px, MAX_W, MAX_H)
        assert _within_bounds(w, h), f"超界：{w_px}x{h_px} -> {w}x{h}"
        assert abs((w / h) - (w_px / h_px)) < 1e-6, f"變形：{w_px}x{h_px}"


def test_regression_original_500_dimensions_now_fit():
    """
    重現原始 500 的像素比例（高度 ~2x 寬度），確認修後尺寸落在頁框內。
    原 bug：map_h ≈ 963pt > frame 728pt。修後必須 <= max_h。
    """
    w, h = _fit_image_dims(760, 1520, MAX_W, MAX_H)
    assert h <= MAX_H + 1e-6
    assert w <= MAX_W + 1e-6
