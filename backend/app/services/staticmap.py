# backend/app/services/staticmap.py
"""
CellTrail 靜態地圖產生器

從 OSM tile server 拉圖磚、拼接成底圖，再把 raw_traces 點位
疊繪上去，回傳 PNG bytes 供 reportlab 嵌入 PDF。

設計決策：
- 不依賴瀏覽器（Selenium/Playwright），純 Python + Pillow 完成。
- 底圖來源：OpenStreetMap tile.openstreetmap.org（CC-BY-SA）。
  每次 PDF 匯出頂多拉 max_side² 張圖磚（預設 4×4=16），對 OSM
  政策而言屬一次性小量請求；User-Agent 帶上聯絡信箱。
- 若 tile fetch 失敗（離線、防火牆），fallback 為灰底，仍繪點位；
  不會因網路問題讓整份報告崩潰。
- Zoom 自動選擇：找最高 zoom 使整個 bounding box 仍在
  max_side × max_side 磚以內。
"""
from __future__ import annotations

import io
import math
import time
from typing import Dict, List, Optional, Tuple

import requests
from PIL import Image, ImageDraw

TILE_SIZE = 256
_UA = "CellTrail/0.2 (+chen95572295@gmail.com)"
_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

_PALETTE = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
    "#42d4f4", "#f032e6", "#bfef45", "#469990", "#9a6324",
]


# ── 座標數學 ──────────────────────────────────────────────────

def _to_float_tile(lat: float, lon: float, z: int) -> Tuple[float, float]:
    """Web Mercator 小數 tile 座標（Slippy map convention）。"""
    n = 2.0 ** z
    lat_r = math.radians(lat)
    fx = (lon + 180.0) / 360.0 * n
    fy = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return fx, fy


def _pick_zoom(
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    max_side: int = 4, z_lo: int = 9, z_hi: int = 16,
) -> int:
    """找最高 zoom 使 bbox 的圖磚數 ≤ max_side × max_side。"""
    for z in range(z_hi, z_lo - 1, -1):
        x0 = int(_to_float_tile(lat_max, lon_min, z)[0])
        y0 = int(_to_float_tile(lat_max, lon_min, z)[1])
        x1 = int(_to_float_tile(lat_min, lon_max, z)[0])
        y1 = int(_to_float_tile(lat_min, lon_max, z)[1])
        if (x1 - x0 + 1) <= max_side and (y1 - y0 + 1) <= max_side:
            return z
    return z_lo


# ── 圖磚抓取 ──────────────────────────────────────────────────

def _fetch_tile(z: int, x: int, y: int) -> Optional[Image.Image]:
    """抓一張 OSM 圖磚；失敗回 None（灰色 placeholder 由呼叫端處理）。"""
    url = _TILE_URL.format(z=z, x=x, y=y)
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=6)
        if r.status_code == 200:
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as exc:
        print(f"[staticmap] fetch failed {url}: {exc}")
    return None


# ── 顏色工具 ──────────────────────────────────────────────────

def _hex_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# ── 主函式 ────────────────────────────────────────────────────

def build_map_image(
    points: List[Dict],
    output_w: int = 760,
    max_side: int = 4,
) -> Optional[bytes]:
    """
    points: list of {"lat": float, "lng": float, "target_id": str, ...}
    Returns PNG bytes, or None if no located points.
    """
    pts = [p for p in points if p.get("lat") and p.get("lng")]
    if not pts:
        return None

    lats = [p["lat"] for p in pts]
    lngs = [p["lng"] for p in pts]

    # Bounding box，至少 0.003° 邊距（台灣緯度約 300m）
    pad_lat = max((max(lats) - min(lats)) * 0.15, 0.003)
    pad_lng = max((max(lngs) - min(lngs)) * 0.15, 0.004)
    lat_min = min(lats) - pad_lat
    lat_max = max(lats) + pad_lat
    lon_min = min(lngs) - pad_lng
    lon_max = max(lngs) + pad_lng

    zoom = _pick_zoom(lat_min, lat_max, lon_min, lon_max, max_side=max_side)

    # 圖磚網格索引
    x0 = int(_to_float_tile(lat_max, lon_min, zoom)[0])
    y0 = int(_to_float_tile(lat_max, lon_min, zoom)[1])
    x1 = int(_to_float_tile(lat_min, lon_max, zoom)[0])
    y1 = int(_to_float_tile(lat_min, lon_max, zoom)[1])

    n_cols = x1 - x0 + 1
    n_rows = y1 - y0 + 1
    canvas_w = n_cols * TILE_SIZE
    canvas_h = n_rows * TILE_SIZE

    # 底圖拼接（灰底 fallback）
    canvas = Image.new("RGB", (canvas_w, canvas_h), (220, 220, 220))
    total = n_cols * n_rows
    for row, ty in enumerate(range(y0, y1 + 1)):
        for col, tx in enumerate(range(x0, x1 + 1)):
            tile = _fetch_tile(zoom, tx, ty)
            if tile:
                canvas.paste(tile, (col * TILE_SIZE, row * TILE_SIZE))
            if total > 1:
                time.sleep(0.04)  # 禮貌限速：避免 OSM 封鎖

    # 繪製點位
    draw = ImageDraw.Draw(canvas, "RGBA")
    target_ids = list(dict.fromkeys(p["target_id"] for p in pts))  # 保序去重
    color_map = {tid: _PALETTE[i % len(_PALETTE)] for i, tid in enumerate(target_ids)}

    for p in pts:
        fx, fy = _to_float_tile(p["lat"], p["lng"], zoom)
        px = int((fx - x0) * TILE_SIZE)
        py = int((fy - y0) * TILE_SIZE)
        rgb = _hex_rgb(color_map[p["target_id"]])
        r = 6
        draw.ellipse(
            [px - r, py - r, px + r, py + r],
            fill=(*rgb, 210),
            outline=(255, 255, 255, 240),
            width=2,
        )

    # OSM 著作權標注（右下角）
    try:
        draw.text(
            (canvas_w - 4, canvas_h - 4),
            "© OpenStreetMap contributors",
            fill=(30, 30, 30, 180),
            anchor="rb",
        )
    except Exception:
        pass

    # 縮放至目標寬度
    if canvas_w != output_w:
        new_h = int(canvas_h * output_w / canvas_w)
        canvas = canvas.resize((output_w, new_h), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def color_legend(target_ids: List[str]) -> List[Tuple[str, str]]:
    """回傳 [(target_id, hex_color), ...] 供 reportlab Table 使用。"""
    return [(tid, _PALETTE[i % len(_PALETTE)]) for i, tid in enumerate(target_ids)]
