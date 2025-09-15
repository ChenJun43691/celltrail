import csv, os
from functools import lru_cache

DICT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..", "data", "cell_sites_dict.csv"))

def _norm(s: str | None) -> str:
    return (s or "").strip().replace(" ", "").replace("　", "")

@lru_cache(maxsize=1)
def _load_dict():
    by_cell = {}
    by_addr = {}
    if os.path.exists(DICT_PATH):
        with open(DICT_PATH, "r", encoding="utf-8-sig") as f:
            rdr = csv.DictReader(f)
            for r in rdr:
                lat = r.get("lat"); lng = r.get("lng")
                if not lat or not lng:
                    continue
                lat = float(lat); lng = float(lng)
                cid = _norm(r.get("cell_id"))
                addr = _norm(r.get("cell_addr"))
                if cid:
                    by_cell[cid] = (lat, lng)
                if addr:
                    by_addr[addr] = (lat, lng)
    return by_cell, by_addr

def lookup(cell_id: str | None, cell_addr: str | None):
    by_cell, by_addr = _load_dict()
    cid = _norm(cell_id)
    addr = _norm(cell_addr)
    if cid and cid in by_cell:
        return by_cell[cid]
    if addr and addr in by_addr:
        return by_addr[addr]
    return None