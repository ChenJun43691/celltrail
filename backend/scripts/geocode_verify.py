#!/usr/bin/env python3
# backend/scripts/geocode_verify.py
"""
地址地理編碼 + 雙重反查驗證 → 產出可匯入 cell_towers 的 CSV
─────────────────────────────────────────────────────────────────────
2026-07-22

背景
====
`cell_towers` 為空、Google 金鑰無效、OSM 在請求週期內太慢（見 CLAUDE.md 七-10）
的情況下，案件檔上傳後幾乎無法定位。本腳本把地理編碼移到**離線**執行：
慢慢查、嚴格驗證、產出對照表，再由管理員經既有 import API 匯入。

**為什麼一定要驗證（CLAUDE.md 七-11）**
=======================================
Nominatim 是**模糊比對**，設計目標是「盡量給個接近的答案」，而非「查不到就
說查不到」。對台灣中文地址（門牌覆蓋稀疏、路名在各行政區高度重複）尤其危險。

實測（本專案三個真實案件檔）若**不驗證**：
    高雄市鳳山區文福里建國路三段539號 → 落在「路竹區建國路」（偏差約 26 公里）
    高雄市三民區本館里昌裕街1號       → 落在「鳥松區本館路」（路與區都不同）
    高雄市三民區寶玉里皓東路50號      → 落在「三民區春陽街184巷」（區對、路錯）

基地台座標**就是證據**。錯誤座標在地圖上與正確者完全無法分辨 —— 使用者會看到
一條流暢合理但指向錯誤地點的軌跡。「查不到」可以補資料；「錯了」不會有人發現。
因此本腳本採**寧可少、不可錯**：兩道驗證都過才輸出。

驗證設計
========
  ① 行政區驗證：反查座標所在的區/鄉/鎮，須與原地址一致
  ② 路名驗證  ：反查座標所在的路名，須與原地址的路名相容
實測擋下的量（三個案件檔、前 40 大地址）：
  ① 擋下 3 址 / 6,473 列   ② 再擋下 8 址 / 699 列
  最終通過 19 址 / 6,050 列（佔 14,237 列的 42.5%）

殘留限制（必須讓使用者知道）
============================
- 精度是「**路名正確**」而非「門牌正確」：座標可能落在該路的某處。以基地台
  涵蓋半徑數百公尺而言可接受，但**不得當成精確位置陳述**。
- 產出是**地址推估值，非業者提供的站台座標**。每列 memo 均標註，匯入時
  請一併填寫 `source` 以利事後稽核區辨。
- 業者對照表到手後應直接覆蓋（`cell_towers` 為 ON CONFLICT DO UPDATE）。

用法
====
    cd /Users/chenguanjun/Desktop/Python程序開發/CellTrail/backend
    source .venv/bin/activate
    python scripts/geocode_verify.py <檔案或資料夾> [...] -o out.csv [--limit N]

    # 只處理列數最多的前 40 個地址（依影響力排序，適合先驗證成效）
    python scripts/geocode_verify.py ~/歷程檔/ -o towers.csv --limit 40

輸出 CSV 欄位為 `cell_id,lat,lng,memo`，可直接由 admin.html →
基地台座標表 → 匯入，或 POST /api/cell-towers/import。

注意：本腳本會實際連線 Nominatim，並嚴守其 1 req/s 使用政策
（每址最多 4 次查詢 + 1 次反查，故約 5 秒/址）。請勿並行執行多份。
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql://unused@localhost:5432/unused")
os.environ.setdefault("GEO_GOOGLE_ENABLED", "0")
os.environ["GEO_OSM_FALLBACK"] = "1"          # 本腳本的存在意義就是查 OSM

NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
SLEEP = 1.1                                    # Nominatim 政策：1 req/s

# 內政部國土測繪中心的行政區反查：免申請、免金鑰，回傳縣市／鄉鎮／村里／地段。
# 為什麼拿它當**主要**驗證器（2026-08-22）：這是官方行政區界資料，
# 而 OSM 的 suburb/city_district 是社群標註、對台灣的覆蓋與命名都不穩定。
# 驗證器本身不準，就等於沒有驗證 —— 而驗證正是七-11 之後唯一擋得住錯誤座標的東西。
NLSC_REVERSE = "https://api.nlsc.gov.tw/other/TownVillagePointQuery"
NLSC_SLEEP = 0.3                               # 官方未載明速率上限，仍保守節流


def _nlsc_ssl_context():
    """NLSC 的伺服器憑證缺少 Subject Key Identifier，Python 3.13 起預設開啟的
    `VERIFY_X509_STRICT` 會直接拒連（curl 則照連）。

    **只關掉 strict 這個旗標，不關驗證**：憑證鏈驗證（verify_mode=CERT_REQUIRED）與
    主機名檢查（check_hostname=True）都保留。用 `ssl._create_unverified_context()`
    圖方便會讓中間人得以竄改反查結果 —— 而反查結果正是我們用來判斷「這個座標可不可信」
    的依據，驗證器本身被騙，整套驗證就失去意義（七-11 的教訓）。
    """
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx

# memo 是**證據來源標註**，會逐列寫進 cell_towers 並在稽核時被讀。
# 它必須同時如實反映「座標哪裡來」與「用什麼驗過」—— 兩者是獨立選項（--provider / --verifier），
# 所以 memo 也必須由兩者組合而成，不能寫死。曾經寫死成 OSM 版，結果用 NLSC 驗證時
# memo 仍宣稱「路名雙重反查驗證(OSM)」，那是對稽核者說了不實的話。
_SRC_LABEL = {
    "osm":    "地址推估座標(OSM)",
    "google": "地址地理編碼座標(Google)",
    "tgos":   "TGOS門牌定位座標(內政部門牌資料)",
}
_VER_LABEL = {
    "osm":  "已通過行政區+路名雙重反查驗證(OSM)",
    "nlsc": "已通過行政區反查驗證(內政部NLSC)",
}


def build_memo(provider: str, verifier: str) -> str:
    return f"{_SRC_LABEL[provider]}｜{_VER_LABEL[verifier]}｜非業者提供"


MEMO = build_memo("osm", "osm")                 # 向後相容：舊預設組合

_ADMIN_RE = re.compile(r"^(.{2,3}[市縣])(.{1,4}?[區鄉鎮市])")
_ROAD_RE = re.compile(r"([^區鄉鎮里鄰]{1,6}?(?:路|街|大道))")


def admin_of(addr: str):
    """取出地址的（縣市, 區）。取不到即無法驗證 → 該址一律不採用。"""
    m = _ADMIN_RE.match(addr or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def road_of(addr: str):
    """取出地址的路名。

    必須**先切掉行政區前綴**再找路名：台灣有「路竹區」「三民區」這類含
    「路」字的區名，直接全字串搜尋會把「高雄市路」當成路名（實測踩雷），
    導致驗證基準本身就是錯的。
    字元類別另排除 區/鄉/鎮/里/鄰，避免把里名吃進路名。
    """
    s = addr or ""
    m = _ADMIN_RE.match(s)
    rest = s[m.end():] if m else s
    m2 = _ROAD_RE.search(rest)
    return m2.group(1) if m2 else None


_VILLAGE_RE = re.compile(r"[市縣].{1,4}?[區鄉鎮市]([一-鿿]{1,4}里)")


def village_of(addr: str):
    """取出地址中的里名（例：高雄市鳳山區**文福里**建國路三段539號）。沒有回 None。

    只認「區/鄉/鎮之後緊接的里」，避免把路名裡的「里」字（如「里港路」）誤判。
    """
    m = _VILLAGE_RE.search(addr or "")
    return m.group(1) if m else None


def village_verdict(addr: str, got_village):
    """比對「地址裡的里」與「反查回來的里」→ 'ok' / 'mismatch' / 'unknown'。

    抽成純函式的理由與 `roads_compatible` 相同：判定規則是這支腳本裡最容易寫錯、
    也最需要被釘住的部分，埋在 main() 的迴圈裡就測不到。

    'unknown' 的兩種來源都必須當作「無從判斷」而非「通過」：
      - 地址沒寫里（本專案 79.2% 的地址如此）
      - NLSC 沒回里（外島、新開發區等）
    把無從判斷當通過，會讓報告顯示「全部驗過了」而實際上有一半沒驗到。
    """
    want = village_of(addr)
    if not want or not got_village:
        return "unknown"
    return "ok" if want == got_village.strip() else "mismatch"


def strip_village(addr: str) -> str:
    """
    移除「N鄰」與「X里」——行政區劃，非郵遞地址的一部分，但業者常寫進地址欄。
    字元類別必須排除行政層級用字，否則 {1,3} 會貪婪吃進前一級的「區」，
    把「鳳山區文福里」砍成「鳳山」而破壞地址結構（實測踩雷點）。
    """
    s = re.sub(r"\d+鄰", "", addr or "")
    t = re.sub(r"[^市縣區鄉鎮里鄰\d]{1,3}里", "", s)
    return t if re.search(r"[路街道段巷弄]", t) else s


def roads_compatible(want: str | None, got: str | None) -> bool:
    """
    路名相容判定：其一為另一之子字串即可。
    為何允許子字串而非全等：反查常回傳更細的層級（「大豐一路288巷」對
    「大豐一路」、「義華路272巷」對「義華路」），那是同一條路的細分，不算錯。
    """
    if not want or not got:
        return False
    return want in got or got in want


def _get(url: str, ua: str):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None
    finally:
        time.sleep(SLEEP)       # 節流必須對每次請求生效（含失敗），見 geocode.py 同款修正


def reverse(lat: float, lng: float, ua: str):
    """反查座標 → (行政區, 路名)。用於驗證，而非用於產生座標。"""
    d = _get(NOMINATIM_REVERSE + "?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lng, "format": "json", "zoom": 17}), ua)
    if not d:
        return None, None
    a = d.get("address", {})
    dist = (a.get("suburb") or a.get("city_district") or a.get("town")
            or a.get("village") or a.get("county") or "")
    return dist, (a.get("road") or "")


def reverse_nlsc(lat: float, lng: float):
    """用 NLSC 官方 API 反查 → (縣市, 鄉鎮區, 村里)。查不到回 (None, None, None)。

    回傳的是 XML（非 JSON），故不共用 `_get`。

    **村里為什麼取回來、卻不當作預設的拒絕條件**（2026-08-22）：
      取回來的理由——業者地址有 20.8% 帶里名（涵蓋 51.4% 的列），而里比區細得多，
      是能分辨「同一區內定位到錯誤街廓」的唯一免費訊號。
      不預設拒絕的理由——里界附近的建物反查到相鄰里是正常現象，
      硬性拒絕會誤殺正確結果、白白損失覆蓋率。
    折衷：一律取回並在報告中揭露不一致，另提供 `--strict-village` 讓需要時可強制拒絕。
    """
    url = f"{NLSC_REVERSE}/{lng}/{lat}/4326"
    req = urllib.request.Request(url, headers={"User-Agent": "CellTrail-geocode-verify/1.1"})
    try:
        with urllib.request.urlopen(req, timeout=15, context=_nlsc_ssl_context()) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:
        return None, None, None
    finally:
        time.sleep(NLSC_SLEEP)
    cty = re.search(r"<ctyName>(.*?)</ctyName>", body)
    twn = re.search(r"<townName>(.*?)</townName>", body)
    vil = re.search(r"<villageName>(.*?)</villageName>", body)
    return (cty.group(1).strip() if cty else None,
            twn.group(1).strip() if twn else None,
            vil.group(1).strip() if vil else None)


def collect(paths):
    """解析檔案 → {清洗後地址: (列數, {cell_id})}。geocode 全樁掉，只要地址。"""
    from app.services import carrier_profile, geocode as G
    carrier_profile._HEADER_MAP_CACHE = carrier_profile._ingest_fallback_map()
    G.lookup_bulk = lambda k, *a, **kw: {}
    G.lookup = lambda *a, **kw: None
    from app.services.geocode import _simplify_addr
    from app.services import ingest

    files = []
    for p in paths:
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p))
                      if not f.startswith((".", "~$"))
                      and f.lower().endswith((".xlsx", ".xltx", ".xlsm", ".xltm", ".csv", ".pdf"))]
        elif os.path.isfile(p):
            files.append(p)

    rows = collections.Counter()
    cells = collections.defaultdict(set)
    for fp in files:
        try:
            recs = ingest.parse_file_only("GEOVERIFY", os.path.basename(fp),
                                          open(fp, "rb").read())
        except Exception as e:
            print(f"  [跳過] {os.path.basename(fp)[:40]}：{type(e).__name__}", file=sys.stderr)
            continue
        for r in recs:
            if r.get("lat") is not None:
                continue                      # 已自帶座標，不需查
            a = (str(r.get("cell_addr") or "")).strip()
            cid = (str(r.get("cell_id") or "")).strip()
            if not a:
                continue
            c = _simplify_addr(a)
            if not c:
                continue
            rows[c] += 1
            if cid:
                cells[c].add(cid)
    return rows, cells


def main() -> int:
    ap = argparse.ArgumentParser(description="地址地理編碼 + 雙重驗證 → cell_towers CSV")
    ap.add_argument("paths", nargs="+", help="歷程檔或資料夾")
    ap.add_argument("-o", "--out", required=True, help="輸出 CSV 路徑")
    ap.add_argument("--limit", type=int, default=0,
                    help="只處理列數最多的前 N 個地址（0=全部）")
    ap.add_argument("--email", default=os.getenv("NOMINATIM_EMAIL", ""),
                    help="Nominatim 聯絡信箱（其使用政策建議提供）")
    ap.add_argument("--provider", choices=["osm", "google", "tgos"], default="osm",
                    help="正向地理編碼來源。"
                         "google=Google Geocoding（台灣門牌覆蓋率最高；每月 10,000 次免費額度，"
                         "本專案唯一地址約 3,459 → 一次跑完通常不產生費用）；"
                         "tgos=內政部全國門牌定位（**API 版須綁 1~4 個固定 IP，本專案做不到**）；"
                         "osm=Nominatim（覆蓋率低、且會回看起來正常但錯誤的座標，故驗證不可省）")
    ap.add_argument("--max-requests", type=int, default=9000,
                    help="正向查詢次數上限（預設 9000）。這是**費用護欄**：Google 每月前 10,000 次"
                         "免費，超過才 $5/1000。達到上限即停止並如實印出已查次數，"
                         "不會安靜地一直跑下去。0=不設限（明確表示你接受可能的費用）")
    ap.add_argument("--strict-village", action="store_true",
                    help="村里不符時也拒絕（預設只在報告中揭露）。里界附近的建物反查到相鄰里"
                         "是正常現象，強制拒絕會誤殺正確結果，故預設關閉；"
                         "需要最保守判定時再開。")
    ap.add_argument("--top", type=int, default=10,
                    help="報告最後列出「高影響地址」的筆數（預設 10）。"
                         "本專案前 10 大地址即涵蓋 63%% 的列 —— 這幾筆的正確性幾乎決定整張圖，"
                         "值得逐筆人工確認。")
    ap.add_argument("--verifier", choices=["nlsc", "osm"], default="nlsc",
                    help="反查驗證器。nlsc=內政部官方行政區（免金鑰，預設）；"
                         "osm=Nominatim reverse（含路名比對，但行政區欄位對台灣不穩定）")
    args = ap.parse_args()

    if args.provider == "google":
        # 提早失敗：金鑰無效時 Google 對每一筆都回 REQUEST_DENIED，使用者只會看到
        # 「全部查無結果」，然後懷疑是地址有問題。先用一個公開地址探路，把真正原因講清楚。
        if not os.getenv("GOOGLE_MAPS_API_KEY", "").strip():
            print("✗ --provider google 需要 GOOGLE_MAPS_API_KEY", file=sys.stderr)
            return 2
        os.environ["GEO_GOOGLE_ENABLED"] = "1"
        from app.services.geocode import _google_geocode as _probe
        if _probe("高雄市苓雅區四維三路2號") is None:
            print("✗ Google 金鑰探測失敗（上面應有 status / error_message）。\n"
                  "  常見原因：金鑰已刪除或輪替、未啟用 Geocoding API、或設了 HTTP referrer 限制\n"
                  "  （referrer 限制對伺服器端呼叫無效）。\n"
                  "  修法：GCP Console → APIs & Services → Credentials 建新金鑰並啟用 Geocoding API。",
                  file=sys.stderr)
            return 2

    if args.provider == "tgos":
        # 提早失敗：沒有憑證卻指定 tgos，會安靜地一個都查不到，
        # 使用者只會看到「全部查無結果」而不知道是憑證沒設。
        if not (os.getenv("TGOS_APP_ID", "").strip() and os.getenv("TGOS_API_KEY", "").strip()):
            print("✗ --provider tgos 需要 TGOS_APP_ID 與 TGOS_API_KEY（兩者都要）。\n"
                  "  申請：https://www.tgos.tw → 註冊會員 → 申請「全國門牌地址定位服務」\n"
                  "  （限政府機關／法人／學術單位，免費；客服 tgos@moi.gov.tw）",
                  file=sys.stderr)
            return 2
        os.environ["GEO_TGOS_ENABLED"] = "1"

    ua = f"CellTrail-geocode-verify/1.0 ({args.email})" if args.email \
        else "CellTrail-geocode-verify/1.0"

    from app.services.geocode import _osm_geocode, _tgos_geocode, _google_geocode

    if args.provider == "google":
        # Google 對台灣門牌覆蓋率最高，但它是**通用**地理編碼器 —— 一樣會「盡量給個接近的答案」。
        # 所以驗證絕不能省：下面的 NLSC 官方行政區反查照跑（七-11 的教訓不因來源而異）。
        def forward(a):
            return _google_geocode(a)
    elif args.provider == "tgos":
        # TGOS 是門牌權威來源，不需要（也不該）像 OSM 那樣剝里再猜 —— 它認得里。
        def forward(a):
            return _tgos_geocode(a)
    else:
        def forward(a):
            # 去里版優先（實測命中率高），原式保留為後備；過度剝除只會多一次查無，
            # 不會產生錯誤座標 —— 因為所有結果都還要過下面的驗證。
            for q in ([strip_village(a)] if strip_village(a) != a else []) + [a]:
                hit = _osm_geocode(q)
                if hit:
                    return hit
            return None

    memo = build_memo(args.provider, args.verifier)

    rows, cells = collect(args.paths)
    if not rows:
        print("找不到任何可查詢的地址", file=sys.stderr)
        return 1

    ordered = [a for a, _ in rows.most_common()]      # 依列數排序：先處理高影響地址
    targets = ordered[:args.limit] if args.limit else ordered
    total_rows = sum(rows.values())
    # osm 受 Nominatim 1 req/s 政策約束（每址最多 4 次查詢 + 1 次反查）；
    # google / tgos 無此限制，瓶頸只剩 NLSC 反查的保守節流。
    per = 5 if args.provider == "osm" else 1
    print(f"來源={args.provider}  驗證={args.verifier}")
    print(f"地址 {len(ordered)} 個（{total_rows:,} 列）；本次處理前 {len(targets)} 個"
          f"，預估 {len(targets) * per // 60 + 1} 分鐘\n")

    accepted, rej_dist, rej_road, notfound = {}, [], [], []
    rej_village = []                  # --strict-village 開啟時因里不符而拒絕
    village_warn = []                 # 已採用但里名對不上 → 報告中揭露，交人工判斷
    nlsc_view = {}                    # 採用者的 NLSC 反查結果（供高影響地址報告顯示）
    n_forward = 0                     # 實際送出的正向查詢次數（費用護欄用，且要如實回報）
    for i, a in enumerate(targets, 1):
        city, dist = admin_of(a)
        want_road = road_of(a)
        if args.max_requests and n_forward >= args.max_requests:
            print(f"\n  ⚠ 已達 --max-requests 上限 {args.max_requests}，停止查詢"
                  f"（尚有 {len(targets) - i + 1} 個地址未處理）。", flush=True)
            targets = targets[:i - 1]
            break
        n_forward += 1
        hit = forward(a)
        if not hit:
            notfound.append(a)
        elif args.verifier == "nlsc":
            # 官方行政區反查：縣市與鄉鎮都必須對得上。
            # 這裡**不比路名** —— NLSC 這支 API 不回路名，而硬要再打一次 OSM 拿路名
            # 等於把不可靠的來源重新引回驗證鏈，得不償失。
            got_cty, got_twn, got_vil = reverse_nlsc(hit[0], hit[1])
            got_show = f"{got_cty or '?'}{got_twn or '?'}{got_vil or ''}"
            want_vil = village_of(a)
            vil_bad = village_verdict(a, got_vil) == "mismatch"
            if not city or not dist:
                rej_dist.append((a, "地址無法解析出縣市/區，無從驗證"))
            elif (got_cty or "").replace("臺", "台") != city.replace("臺", "台"):
                rej_dist.append((a, got_show))
            elif dist not in (got_twn or ""):
                rej_dist.append((a, got_show))
            elif vil_bad and args.strict_village:
                rej_village.append((a, f"{got_show}（地址寫 {want_vil}）"))
            else:
                accepted[a] = hit
                nlsc_view[a] = got_show
                if vil_bad:
                    village_warn.append((a, want_vil, got_vil))
        else:
            got_dist, got_road = reverse(hit[0], hit[1], ua)
            if not dist or dist not in (got_dist or ""):
                rej_dist.append((a, got_dist))
            elif not roads_compatible(want_road, got_road):
                rej_road.append((a, got_road))
            else:
                accepted[a] = hit
        if i % 10 == 0 or i == len(targets):
            print(f"  ..{i}/{len(targets)}  採用 {len(accepted)}"
                  f"  區不符 {len(rej_dist)}  路不符 {len(rej_road)}"
                  f"  里不符 {len(rej_village)}  查無 {len(notfound)}", flush=True)

    def _rows(keys):
        return sum(rows[k] for k in keys)

    acc_rows = _rows(accepted)
    print("\n=== 結果 ===")
    print(f"  正向查詢次數        : {n_forward:>4}"
          + ("（Google 每月前 10,000 次免費）" if args.provider == "google" else ""))
    print(f"  採用（雙重驗證通過）: {len(accepted):>4} 址 / {acc_rows:>7,} 列 "
          f"({acc_rows / total_rows * 100:.1f}%)")
    print(f"  拒絕・行政區不符    : {len(rej_dist):>4} 址 / {_rows(a for a, _ in rej_dist):>7,} 列")
    print(f"  拒絕・路名不符      : {len(rej_road):>4} 址 / {_rows(a for a, _ in rej_road):>7,} 列")
    if rej_village:
        print(f"  拒絕・里名不符      : {len(rej_village):>4} 址 / "
              f"{_rows(a for a, _ in rej_village):>7,} 列（--strict-village）")
    print(f"  查無結果            : {len(notfound):>4} 址 / {_rows(notfound):>7,} 列")

    # ── 已採用但里名對不上：不拒絕，但必須讓人看見 ──
    if village_warn:
        vw_rows = _rows(a for a, _, _ in village_warn)
        print(f"\n  ⚠ 已採用但里名與反查不符：{len(village_warn)} 址 / {vw_rows:,} 列")
        print("     里界附近的建物反查到相鄰里屬正常，但也可能是定位到同區內的錯誤街廓。")
        print("     要一律拒絕請加 --strict-village。")
        for a, want, got in sorted(village_warn, key=lambda x: -rows[x[0]])[:5]:
            print(f"       {rows[a]:>6,} 列  {a[:32]}  地址寫 {want} → 反查 {got}")
    if rej_dist or rej_road:
        print("\n  被驗證擋下的錯誤匹配（若無驗證，這些都會變成錯誤點位）：")
        for a, got in (rej_dist + rej_road)[:5]:
            print(f"    {a[:34]} → 實際落在 {got}")

    # ── 高影響地址：少數幾筆決定整張圖，值得逐筆人工確認 ──
    # 本專案實測：前 10 大地址涵蓋 63.2% 的列；而七-11 記載被 OSM 錯誤定位的兩個地址
    # 合計就佔 19.0%。一個錯誤的高頻座標，會讓地圖上五分之一的點指向錯誤地點，
    # 且看起來完全正常。人工確認 10 筆是可行的，確認 3,459 筆不是。
    if accepted and args.top:
        top_acc = sorted(accepted, key=lambda a: -rows[a])[:args.top]
        cov = sum(rows[a] for a in top_acc)
        print(f"\n=== 高影響地址（前 {len(top_acc)} 名，涵蓋已採用結果的 "
              f"{cov / max(acc_rows, 1) * 100:.1f}%）—— 請逐筆目視確認 ===")
        for a in top_acc:
            lat, lng = accepted[a]
            flag = ""
            wv, gv = village_of(a), None
            for _a, _w, _g in village_warn:
                if _a == a:
                    gv = _g
            if gv:
                flag = f"  ⚠里名不符(地址寫{wv}→反查{gv})"
            print(f"  {rows[a]:>6,} 列  {a[:40]}")
            print(f"          → {lat:.6f}, {lng:.6f}   反查={nlsc_view.get(a, '—')}{flag}")
            print(f"          → Google Maps 目視：https://www.google.com/maps?q={lat:.6f},{lng:.6f}")

    n = 0
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["cell_id", "lat", "lng", "memo"])
        for a, (lat, lng) in accepted.items():
            for cid in sorted(cells[a]):
                w.writerow([cid, f"{lat:.7f}", f"{lng:.7f}", memo])
                n += 1
    print(f"\n產出 {n} 筆 cell_id 對應 → {args.out}")
    print("匯入：admin.html → 基地台座標表 → 匯入 CSV"
          f"（建議 source 填「{_SRC_LABEL[args.provider]}+{_VER_LABEL[args.verifier]}」以利稽核區辨）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
