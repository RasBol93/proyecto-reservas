from typing import Any, Dict, List

import gspread

from app.sheets import read_records_manual
from app.utils import to_bool


def load_content_map(orders_sh: gspread.Spreadsheet) -> Dict[str, str]:
    ws = orders_sh.worksheet("Content")
    rows = read_records_manual(ws, required_headers=["key", "value", "active"])
    out: Dict[str, str] = {}
    for r in rows:
        if not to_bool(r.get("active", "")):
            continue
        k = str(r.get("key", "")).strip().lower()
        v = str(r.get("value", "")).strip()
        if k:
            out[k] = v
    return out


def load_faq_list(orders_sh: gspread.Spreadsheet) -> List[Dict[str, Any]]:
    ws = orders_sh.worksheet("FAQ")
    rows = read_records_manual(ws, required_headers=["id", "question", "answer", "active", "priority"])
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not to_bool(r.get("active", "")):
            continue
        fid = str(r.get("id", "")).strip()
        q = str(r.get("question", "")).strip()
        a = str(r.get("answer", "")).strip()
        try:
            p = int(str(r.get("priority", "")).strip() or "999")
        except Exception:
            p = 999
        if fid and q and a:
            out.append({"id": fid, "question": q, "answer": a, "priority": p})
    out.sort(key=lambda x: x["priority"])
    return out
