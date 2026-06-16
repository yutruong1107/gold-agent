import os
import json
import html
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from dotenv import load_dotenv

from greennode_agentbase import (
    GreenNodeAgentBaseApp,
    RequestContext,
    PingStatus,
)

load_dotenv()

app = GreenNodeAgentBaseApp()

LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")

BTMC_API = "http://api.btmc.vn/api/BTMCAPI/getpricebtmc?key=3kd8ub1llcg9t45hnoh8hmn7t5kc2v"
GOLD_API = "https://api.gold-api.com/price/XAU"          # spot USD/oz (no key)
FX_API = "https://open.er-api.com/v6/latest/USD"         # USD/VND (no key)
OZ_PER_LUONG = 1.2057                                    # 1 lượng = 37.5g ≈ 1.2057 oz

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_history.json")
MEMORY_ID = os.environ.get("MEMORY_ID", "")
MEMORY_NS = os.environ.get("MEMORY_NS", "")
NEWS_KEYWORDS = ["giá vàng hôm nay", "vàng SJC", "giá vàng tăng giảm"]

# Chuỗi lịch sử ƯỚC LƯỢNG (triệu đồng/lượng) — nội suy từ diễn biến thị trường,
# sẽ bị ghi đè dần bằng dữ liệu thật khi agent chạy mỗi ngày.
# (date, sjc_sell_M_luong, intl_M_luong)
SEED_SERIES = [
    ("2026-01-02", 170, 152),
    ("2026-01-29", 191, 170),   # SJC đỉnh
    ("2026-02-10", 181, 153),
    ("2026-02-28", 178, 149),
    ("2026-03-15", 170, 140),   # premium đỉnh ~30M (T3)
    ("2026-03-31", 167, 140),
    ("2026-04-15", 161, 141),
    ("2026-04-30", 160, 140),
    ("2026-05-15", 165, 143),
    ("2026-05-31", 160, 142),
    ("2026-06-05", 155, 140),
    ("2026-06-10", 153, 138),
    ("2026-06-12", 152, 137.5),
    ("2026-06-13", 151.5, 137),
    ("2026-06-14", 151, 137),   # hôm qua (seed) — sẽ bị ghi đè bằng dữ liệu thật
]


# ---------------- History cache ----------------

def _load_history() -> dict:
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_history(hist: dict) -> None:
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


MEMORY_BASE = "https://agentbase.api.vngcloud.vn/memory/memories"
IAM_TOKEN_URL = "https://iam.api.vngcloud.vn/accounts-api/v2/auth/token"


def _gn_creds():
    cid = os.environ.get("GREENNODE_CLIENT_ID", "")
    csec = os.environ.get("GREENNODE_CLIENT_SECRET", "")
    if not (cid and csec):
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".greennode.json"), encoding="utf-8") as f:
                j = json.load(f)
            cid, csec = j.get("client_id", ""), j.get("client_secret", "")
        except Exception:
            pass
    return cid, csec


def _gn_token():
    import base64
    cid, csec = _gn_creds()
    if not (cid and csec):
        return None
    try:
        auth = base64.b64encode(f"{cid}:{csec}".encode()).decode()
        req = urllib.request.Request(
            IAM_TOKEN_URL, data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")).get("access_token")
    except Exception:
        return None


MEMORY_ACTOR = "gold"
MEMORY_SESSION = "prices"


def mem_browse_history() -> dict:
    """Đọc lịch sử giá thật từ AgentBase Memory Events (lưu nguyên văn). Trả {date:{buy,sell,intl}}."""
    if not MEMORY_ID:
        return {}
    tok = _gn_token()
    if not tok:
        return {}
    try:
        url = (f"{MEMORY_BASE}/{MEMORY_ID}/actors/{MEMORY_ACTOR}/sessions/{MEMORY_SESSION}"
               f"/events?page=1&size=100")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        evts = data.get("listData") or data.get("data") or (data if isinstance(data, list) else [])
        hist = {}
        for e in evts:
            p = e.get("payload") if isinstance(e, dict) else None
            s = (p.get("message") if isinstance(p, dict) else None) or (e.get("message") if isinstance(e, dict) else None) or (e.get("content") if isinstance(e, dict) else None)
            if not s or not s.startswith("PRICE|"):
                continue
            try:
                date = s.split("|")[1]
                kv = dict(p2.split("=", 1) for p2 in s.split("|")[2:] if "=" in p2)
                hist[date] = {"buy": float(kv["buy"]), "sell": float(kv["sell"]), "intl": float(kv["intl"])}
            except Exception:
                continue
        return hist
    except Exception:
        return {}


def mem_insert_today(date, buy, sell, intl) -> bool:
    if not MEMORY_ID:
        return False
    tok = _gn_token()
    if not tok:
        return False
    try:
        url = f"{MEMORY_BASE}/{MEMORY_ID}/actors/{MEMORY_ACTOR}/sessions/{MEMORY_SESSION}/events"
        body = json.dumps({"payload": {"type": "conversational", "role": "assistant",
                                       "message": f"PRICE|{date}|buy={buy}|sell={sell}|intl={intl}"}}).encode()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception:
        return False


# ---------------- Data fetchers ----------------

def fetch_sjc_today():
    """Giá vàng miếng SJC hôm nay (triệu đồng/lượng) từ BTMC. Trả (buy, sell) hoặc None."""
    try:
        req = urllib.request.Request(BTMC_API, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for row in data.get("DataList", {}).get("Data", []):
            idx = row.get("@row")
            name = row.get(f"@n_{idx}", "").upper()
            if "VÀNG MIẾNG SJC" in name or ("SJC" in name and "VÀNG" in name):
                buy = int(float(row.get(f"@pb_{idx}", "0") or 0)) * 10 / 1e6   # chỉ→lượng→triệu
                sell = int(float(row.get(f"@ps_{idx}", "0") or 0)) * 10 / 1e6
                if sell > 0:
                    return round(buy, 2), round(sell, 2)
        return None
    except Exception:
        return None


def fetch_intl_today():
    """Giá vàng quốc tế quy đổi (triệu đồng/lượng) = spot USD/oz × USD/VND × oz/lượng."""
    try:
        req = urllib.request.Request(GOLD_API, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            spot = float(json.loads(resp.read().decode("utf-8"))["price"])  # USD/oz
        req2 = urllib.request.Request(FX_API, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=20) as resp:
            usdvnd = float(json.loads(resp.read().decode("utf-8"))["rates"]["VND"])
        return round(spot * usdvnd * OZ_PER_LUONG / 1e6, 2), round(spot, 2), round(usdvnd, 1)
    except Exception:
        return None, None, None


def _http_text(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
    t = __import__("re").sub(r"<script.*?</script>", "", raw, flags=16)
    t = __import__("re").sub(r"<style.*?</style>", "", t, flags=16)
    t = __import__("re").sub(r"<[^>]+>", " ", t)
    return raw, __import__("re").sub(r"\s+", " ", html.unescape(t))


def _vnum(s):
    s = s.strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def fetch_vnexpress():
    """Đối chiếu giá từ bài 'giá vàng' mới nhất của VnExpress (nguồn độc lập)."""
    import re
    try:
        raw, _ = _http_text("https://vnexpress.net/chu-de/gia-vang-1403")
        arts = [a for a in dict.fromkeys(re.findall(r'href="(https://vnexpress\.net/[a-z0-9-]+\.html)"', raw))
                if "vang" in a][:6]
        out = {"sjc": None, "world": None, "url": None, "title": None}
        for a in arts:
            araw, txt = _http_text(a)
            if out["world"] is None:
                w = re.search(r"([\d.,]+)\s*USD[^.]{0,15}ounce", txt)
                if w:
                    out["world"] = _vnum(w.group(1))
            if out["sjc"] is None:
                s = re.search(r"SJC[^.]{0,60}?([\d]{2,3}(?:[.,]\d{1,3})?)\s*tri[ệe]u", txt)
                if s:
                    out["sjc"] = _vnum(s.group(1))
                    out["url"] = a
                    tm = re.search(r"<title>([^<]+)</title>", araw)
                    out["title"] = (tm.group(1).split("-")[0].strip() if tm else "VnExpress")
            if out["sjc"] and out["world"]:
                break
        return out
    except Exception:
        return {}


def build_series():
    """Gộp seed ước lượng + dữ liệu thật tích lũy + điểm hôm nay (live).
    Trả về (series_list, meta) với series sắp xếp theo ngày."""
    merged = {d: {"sjc": s, "intl": i, "estimated": True} for d, s, i in SEED_SERIES}

    # dữ liệu thật bền vững: ưu tiên AgentBase Memory, fallback file cục bộ
    real = mem_browse_history()  # {date:{buy,sell,intl}}
    using_memory = bool(real)
    if not real:
        real = {d: {"sell": r["sjc"], "intl": r["intl"]} for d, r in _load_history().items()}
    for d, rec in real.items():
        merged[d] = {"sjc": rec.get("sell", rec.get("sjc")), "intl": rec["intl"], "estimated": False}

    # điểm hôm nay = live thật
    today = datetime.now().date().isoformat()
    sjc = fetch_sjc_today()
    intl, spot, usdvnd = fetch_intl_today()
    live_ok = sjc is not None and intl is not None
    if live_ok:
        merged[today] = {"sjc": sjc[1], "intl": intl, "estimated": False, "buy": sjc[0]}
        # Ghi vào Memory (bền vững) nếu hôm nay chưa có; luôn lưu file cục bộ làm backup
        if today not in real:
            mem_insert_today(today, sjc[0], sjc[1], intl)
        loc = _load_history(); loc[today] = {"sjc": sjc[1], "intl": intl, "buy": sjc[0]}; _save_history(loc)

    series = []
    for d in sorted(merged.keys()):
        m = merged[d]
        series.append({"date": d, "sjc": m["sjc"], "intl": m["intl"],
                       "premium": round(m["sjc"] - m["intl"], 2), "estimated": m["estimated"]})

    meta = {"live_ok": live_ok, "today": today, "using_memory": using_memory}
    if live_ok:
        meta.update({"spot_usd": spot, "usdvnd": usdvnd, "buy": sjc[0], "sell": sjc[1]})
    return series, meta


def fetch_gold_news_diverse(limit: int = 3) -> list:
    """Tin giá vàng từ nhiều báo khác nhau (ưu tiên publisher riêng biệt) trong whitelist."""
    pool, seen_title = [], set()
    for kw in NEWS_KEYWORDS:
        try:
            url = f"https://news.google.com/rss/search?q={urllib.parse.quote(kw)}&hl=vi&gl=VN&ceid=VN:vi"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                root = ET.fromstring(resp.read().decode("utf-8"))
            for item in root.findall(".//item")[:8]:
                title = (item.findtext("title", "") or "").strip()
                src = (item.findtext("source", "") or "").strip()
                if src and title.endswith(f"- {src}"):  # bỏ đuôi " - <publisher>" lặp
                    title = title[: -(len(src) + 2)].rstrip(" -")
                title = title.replace("Rới", "Rơi").replace("rới", "rơi")  # sửa lỗi gõ thường gặp
                if not title or title in seen_title:
                    continue
                seen_title.add(title)
                pool.append({"title": title, "source": src,
                             "link": (item.findtext("link", "") or "").strip(),
                             "published": (item.findtext("pubDate", "") or "").strip()})
        except Exception:
            continue
    # Ưu tiên mỗi publisher 1 bài để bao quát ≥3 nguồn
    out, used_src = [], set()
    for a in pool:
        if a["source"] and a["source"] not in used_src:
            out.append(a)
            used_src.add(a["source"])
        if len(out) >= limit:
            break
    for a in pool:  # bù nếu chưa đủ
        if len(out) >= limit:
            break
        if a not in out:
            out.append(a)
    return out[:limit]


def build_market_data() -> dict:
    """RAG Data Extraction → JSON phẳng: giá SJC + nhẫn (mua/bán) + tin tức đa nguồn."""
    p = fetch_prices_map()
    sjc, ring = p.get("sjc", {}), p.get("nhan", {})
    news = fetch_gold_news_diverse(3)
    return {
        "market_price_sjc_buy": f"{_vnfmt(sjc.get('buy',0),2)}M" if sjc else None,
        "market_price_sjc_sell": f"{_vnfmt(sjc.get('sell',0),2)}M" if sjc else None,
        "market_price_ring_buy": f"{_vnfmt(ring.get('buy',0),2)}M" if ring else None,
        "market_price_ring_sell": f"{_vnfmt(ring.get('sell',0),2)}M" if ring else None,
        "scraped_news_headlines": [f"{a.get('source','')}: {a.get('title','')}".strip(": ") for a in news],
        "source_note": "Giá: BTMC (api.btmc.vn, gồm SJC + nhẫn). Tin: Google News (VnExpress/VietNamNet/LaoĐộng/Thanh Tra...). SJC/DOJI/PNJ chặn cào trực tiếp.",
        "asof": datetime.now().strftime("%H:%M %d/%m/%Y"),
    }


def fetch_gold_news() -> list:
    articles, seen = [], set()
    for kw in NEWS_KEYWORDS:
        try:
            url = f"https://news.google.com/rss/search?q={urllib.parse.quote(kw)}&hl=vi&gl=VN&ceid=VN:vi"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                root = ET.fromstring(resp.read().decode("utf-8"))
            for item in root.findall(".//item")[:3]:
                title = (item.findtext("title", "") or "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                articles.append({
                    "title": title,
                    "link": (item.findtext("link", "") or "").strip(),
                    "published": (item.findtext("pubDate", "") or "").strip(),
                    "source": (item.findtext("source", "") or "").strip(),
                })
                break
        except Exception:
            continue
    return articles[:3]


# ---------------- Portfolio: pricing, storage, PnL, AI ----------------

import re as _re

# Loại vàng hỗ trợ: khớp tên sản phẩm BTMC
GOLD_TYPES = [
    {"key": "sjc", "label": "Vàng miếng SJC", "match": ["VÀNG MIẾNG SJC"], "exclude": ["NHẪN", "TRANG SỨC"]},
    {"key": "nhan", "label": "Nhẫn tròn trơn", "match": ["NHẪN TRÒN TRƠN", "NHẪN TRÒN"], "exclude": ["TRANG SỨC"]},
    {"key": "nutrang", "label": "Vàng nữ trang", "match": ["TRANG SỨC", "NỮ TRANG"], "exclude": []},
]


def fetch_prices_map() -> dict:
    """Trả {key:{label,buy,sell}} (triệu đồng/lượng) cho các loại vàng hỗ trợ, từ BTMC."""
    out = {}
    try:
        req = urllib.request.Request(BTMC_API, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = data.get("DataList", {}).get("Data", [])
        for gt in GOLD_TYPES:
            buys, sells = [], []
            for row in rows:
                idx = row.get("@row")
                name = row.get(f"@n_{idx}", "").upper()
                # SJC chỉ khớp đúng vàng miếng SJC, không lẫn nhẫn/trang sức
                if any(m in name for m in gt["match"]) and not any(x in name for x in gt.get("exclude", [])):
                    buy = int(float(row.get(f"@pb_{idx}", "0") or 0)) * 10 / 1e6
                    sell = int(float(row.get(f"@ps_{idx}", "0") or 0)) * 10 / 1e6
                    if buy > 0:
                        buys.append(buy)
                    if sell > 0:
                        sells.append(sell)
            if buys:
                out[gt["key"]] = {
                    "label": gt["label"],
                    "buy": round(sum(buys) / len(buys), 3),
                    "sell": round(sum(sells) / len(sells), 3) if sells else round(sum(buys) / len(buys), 3),
                }
    except Exception:
        pass
    return out


# ---- Lưu trữ portfolio per-user qua Memory Events (snapshot mới nhất) ----

def _sanitize_actor(user: str) -> str:
    a = _re.sub(r"[^a-zA-Z0-9_-]", "-", (user or "guest").strip())[:48]
    return a or "guest"


def _mem_event_write(actor: str, session: str, message: str) -> bool:
    if not MEMORY_ID:
        return False
    tok = _gn_token()
    if not tok:
        return False
    try:
        url = f"{MEMORY_BASE}/{MEMORY_ID}/actors/{actor}/sessions/{session}/events"
        body = json.dumps({"payload": {"type": "conversational", "role": "assistant", "message": message}}).encode()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception:
        return False


def _mem_event_latest(actor: str, session: str, prefix: str):
    """Trả message mới nhất bắt đầu bằng prefix (events sort giảm dần theo thời gian)."""
    if not MEMORY_ID:
        return None
    tok = _gn_token()
    if not tok:
        return None
    try:
        url = f"{MEMORY_BASE}/{MEMORY_ID}/actors/{actor}/sessions/{session}/events?page=1&size=100"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        evts = data.get("listData") or data.get("data") or (data if isinstance(data, list) else [])
        for e in evts:
            p = e.get("payload") if isinstance(e, dict) else None
            s = (p.get("message") if isinstance(p, dict) else None) or (e.get("message") if isinstance(e, dict) else None)
            if s and s.startswith(prefix):
                return s
        return None
    except Exception:
        return None


def pf_load(user: str) -> list:
    s = _mem_event_latest(_sanitize_actor(user), "portfolio", "PF|")
    if not s:
        return []
    try:
        return json.loads(s[3:])
    except Exception:
        return []


def pf_save(user: str, holdings: list) -> bool:
    return _mem_event_write(_sanitize_actor(user), "portfolio", "PF|" + json.dumps(holdings, ensure_ascii=False))


def compute_portfolio(holdings: list, prices: dict) -> dict:
    """Tính giá trị hiện tại, PnL, ROI từng holding + tổng. Giá quy về triệu đồng/lượng."""
    rows, tot_cost, tot_val = [], 0.0, 0.0
    for h in holdings:
        try:
            key = h.get("type", "sjc")
            qty = float(h.get("qty", 0))          # lượng
            buy_price = float(h.get("buy_price", 0))  # triệu/lượng
            pinfo = prices.get(key, {})
            cur = float(pinfo.get("buy", 0))      # tiệm mua lại = số tiền bạn nhận khi bán
            cost = buy_price * qty
            value = cur * qty
            pnl = value - cost
            roi = (pnl / cost * 100) if cost else 0
            tot_cost += cost
            tot_val += value
            rows.append({
                "id": h.get("id"), "type": key, "label": pinfo.get("label", key),
                "qty": qty, "unit": "lượng", "buy_price": buy_price, "buy_date": h.get("buy_date", ""),
                "current": round(cur, 3), "cost": round(cost, 3), "value": round(value, 3),
                "pnl": round(pnl, 3), "roi": round(roi, 2),
            })
        except Exception:
            continue
    tot_pnl = tot_val - tot_cost
    tot_qty = sum(r["qty"] for r in rows)
    totals = {
        "cost": round(tot_cost, 3), "value": round(tot_val, 3),
        "pnl": round(tot_pnl, 3), "roi": round((tot_pnl / tot_cost * 100) if tot_cost else 0, 2),
        "qty": round(tot_qty, 3),
        "avg_buy_price": round(tot_cost / tot_qty, 3) if tot_qty else 0,   # giá mua TB / lượng
        "avg_current_price": round(tot_val / tot_qty, 3) if tot_qty else 0,
    }
    return {"rows": rows, "totals": totals}


def rule_insight(summary: dict) -> str:
    """Nhận định nhanh (fallback, không cần LLM) — tone trấn an, ngắn gọn."""
    t = summary.get("totals", {})
    pnl, roi = t.get("pnl", 0), t.get("roi", 0)
    if not summary.get("rows"):
        return "Bạn chưa có khoản vàng nào. Thêm khoản đầu tiên để tôi theo dõi lời/lỗ giúp bạn mỗi ngày."
    if pnl > 0:
        return (f"Danh mục đang LÃI {pnl:,.1f} triệu (+{roi:.1f}%). Bạn đang đi đúng hướng — "
                f"vàng vốn là kênh giữ giá dài hạn, cứ giữ vững tâm lý.").replace(",", ".")
    if pnl < 0:
        return (f"Danh mục tạm LỖ {abs(pnl):,.1f} triệu ({roi:.1f}%). Đừng lo — vàng biến động ngắn hạn là "
                f"bình thường, giá trị dài hạn vẫn ổn định. Theo dõi thêm vài phiên trước khi quyết định.").replace(",", ".")
    return "Danh mục đang hòa vốn. Tôi sẽ tiếp tục theo dõi và báo bạn khi có biến động đáng chú ý."


def _bold_html(t: str) -> str:
    """Bold + tô màu các điểm chính: tên loại vàng (bold), số tiền/ROI (bold, xanh nếu tăng + / đỏ nếu giảm −)."""
    if not t:
        return t
    t = html.escape(t)
    for lbl in ("Vàng miếng SJC", "Nhẫn tròn trơn", "Vàng nữ trang / trang sức", "Vàng nữ trang"):
        t = t.replace(lbl, f"<b>{lbl}</b>")

    def repl(m):
        tok = m.group(1)
        s = tok.lstrip()
        neg = s.startswith("-") or s.startswith("−")
        pos = s.startswith("+")
        if tok.rstrip().endswith("%"):
            # % luôn là ROI: âm = đỏ, còn lại (dương/không dấu) = xanh
            return f'<b class="{"pnl-neg" if neg else "pnl-pos"}">{tok}</b>'
        # số tiền M: chỉ tô màu khi có dấu +/− (giá trung bình/tổng không dấu = trung tính)
        if pos:
            return f'<b class="pnl-pos">{tok}</b>'
        if neg:
            return f'<b class="pnl-neg">{tok}</b>'
        return f"<b>{tok}</b>"

    # số tiền dạng 143,25M / +21,0M / −1,0M và phần trăm +6,5% / -1,3%
    t = _re.sub(r"([+\-−]?\d[\d.]*(?:,\d+)?\s*[M%])", repl, t)
    return t


def _vnfmt(x, dec=1) -> str:
    """Định dạng số kiểu VN: phẩy thập phân, chấm hàng nghìn. 1036.25 -> '1.036,25'."""
    try:
        s = f"{float(x):,.{dec}f}"  # '1,036.25'
        return s.replace(",", "§").replace(".", ",").replace("§", ".")
    except Exception:
        return str(x)


AI_SYSTEM = (
    "Bạn tên là Aurum (lấy từ tên Latin của vàng, ký hiệu Au) — AI Wealth Management Agent (trợ lý quản lý tài sản) cao cấp trên nền tảng GreenNode AgentBase, "
    "vận hành như một nhà phân tích tài chính chuyên nghiệp cho ứng dụng 'Gold Companion' (tích sản vàng tại Việt Nam). "
    "Tự động đối chiếu danh mục user + tin tức vĩ mô realtime + giá thị trường để đưa nhận định mang tính HÀNH ĐỘNG (actionable).\n\n"
    "QUY TẮC ĐỊNH DẠNG:\n"
    "- Tiếng Việt, văn phong chuyên nghiệp, đáng tin cậy, định hướng chiến lược nhưng gần gũi, cá nhân hóa.\n"
    "- Dùng chữ 'M' viết hoa thay cho 'triệu đồng' (vd: 592,0M).\n"
    "- Dấu phẩy cho thập phân, dấu chấm cho hàng nghìn (vd: 3,32% ; 143,25M ; 1.036,0M). Số ROI/% khớp ĐÚNG 2 chữ số thập phân với bảng (vd 6,21%).\n"
    "- Viết tiếng Việt CHUẨN CHÍNH TẢ tuyệt đối, đặt dấu thanh đúng (vd: 'rời khỏi', 'rơi'; KHÔNG viết sai thành 'rới').\n\n"
    "TƯ DUY & ĐỘ DÀI (NGẮN GỌN, CLEAN):\n"
    "- Giới hạn TỐI ĐA 2-3 câu. Văn phong sang trọng, cô đọng.\n"
    "- TUYỆT ĐỐI KHÔNG liệt kê lại các số tĩnh đã có trên UI (số lượng lượng, giá mua/ROI TỪNG lệnh). Có thể nhắc 1 chỉ số tổng (ROI tổng) ở mức vĩ mô.\n"
    "- Tổng hợp tin tức (scraped_news_headlines) thành 1 nhận định VĨ MÔ về bản chất thị trường, rồi GỢI Ý 1 hướng hành động tiếp theo (tối ưu giá vốn lệnh lỗ / tích sản thêm).\n"
    "- KHÔNG tự tính số mới. Nhận định mang tính tham khảo, KHÔNG dùng từ 'chắc chắn lãi/cam kết/đảm bảo lợi nhuận'.\n"
    "- LOGIC TÀI CHÍNH PHẢI ĐÚNG: căng thẳng/bất ổn địa chính trị leo thang → vàng TĂNG (kênh trú ẩn an toàn). KHÔNG được nói 'thỏa thuận hòa bình' làm vàng tăng. "
    "Khi nhắc yếu tố vĩ mô, dùng cụm trung tính như 'căng thẳng địa chính trị leo thang' hoặc 'bất ổn vĩ mô', tránh gán nhân-quả sai.\n"
    "- Kết bằng 1 câu hỏi mở mời hành động.\n\n"
    "MẪU ĐẦU RA KỲ VỌNG (đúng 2-3 câu):\n"
    "\"Chào [user], danh mục tổng của bạn đang tăng trưởng tốt (+[roi]%). Dù thị trường có rung lắc ngắn hạn do [tin tức chính], xu hướng dài hạn vẫn rất ổn định. "
    "Bạn có muốn tối ưu giá vốn cho các lệnh đang tạm lỗ không?\""
)


def _ai_context(summary: dict, user_name: str, news=None) -> str:
    t = summary.get("totals", {})
    rows = summary.get("rows", [])
    details = "; ".join(
        f"{r['label']} ({_vnfmt(r['qty'],1)} lượng): ROI {'+' if r['roi'] >= 0 else '−'}{_vnfmt(abs(r['roi']),2)}%"
        for r in rows)
    dc = t.get("day_change")
    if dc is None:
        daily = "chưa có dữ liệu phiên trước"
    elif dc > 0:
        daily = f"tăng +{_vnfmt(dc)}M"
    elif dc < 0:
        daily = f"giảm −{_vnfmt(abs(dc))}M"
    else:
        daily = "gần như không đổi (0,0M)"
    pr = summary.get("prices") or {}
    sjc, ring = pr.get("sjc", {}), pr.get("nhan", {})
    market = (f"SJC mua vào {_vnfmt(sjc.get('buy',0),2)}M / bán ra {_vnfmt(sjc.get('sell',0),2)}M"
              if sjc else "không có dữ liệu")
    if ring:
        market += f"; Nhẫn mua vào {_vnfmt(ring.get('buy',0),2)}M / bán ra {_vnfmt(ring.get('sell',0),2)}M"
    news_lines = ""
    if news:
        news_lines = "\n".join(f"   {i}. {n}" for i, n in enumerate(news[:3], 1))
    return (f"- user_name: {user_name}\n"
            f"- total_asset: {_vnfmt(t.get('value',0))}M\n"
            f"- total_pnl: {_vnfmt(t.get('pnl',0))}M\n"
            f"- total_roi: {_vnfmt(t.get('roi',0),2)}%\n"
            f"- daily_movement: {daily}\n"
            f"- avg_buy_price: {_vnfmt(t.get('avg_buy_price',0),2)}M\n"
            f"- market_price_sjc: {market}\n"
            f"- portfolio_details: [{details}]\n"
            f"- scraped_news_headlines:\n{news_lines if news_lines else '   (không có)'}")


def ai_insight(summary: dict, question: str = "", user_name: str = "bạn", news=None) -> str:
    """Nhận định/chat cá nhân hóa (gemma) — RAG tin tức + giá thị trường + danh mục. Fallback rule nếu LLM lỗi."""
    if not (LLM_MODEL and LLM_BASE_URL and LLM_API_KEY) or not summary.get("rows"):
        return rule_insight(summary)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, timeout=45, max_retries=0)
        data = _ai_context(summary, user_name, news)
        if question:
            usr = (data + f"\n\nNgười dùng hỏi: \"{question}\"\n"
                   "Trả lời trực tiếp câu hỏi theo đúng quy tắc định dạng và tone ở trên, ngắn gọn 2-4 câu.")
        else:
            usr = data + "\n\nHãy xuất câu nhận định chuẩn UX theo MẪU ĐẦU RA, dùng đúng dữ liệu trên."
        r = client.chat.completions.create(
            model=LLM_MODEL, max_tokens=900,
            messages=[{"role": "system", "content": AI_SYSTEM}, {"role": "user", "content": usr}])
        msg = r.choices[0].message
        txt = (msg.content or "").strip() or (getattr(msg, "reasoning", "") or "").strip()
        return txt or rule_insight(summary)
    except Exception:
        return rule_insight(summary)


# ---------------- Stats ----------------

def compute_stats(series):
    if not series:
        return {}
    sjc_peak = max(series, key=lambda x: x["sjc"])
    prem_peak = max(series, key=lambda x: x["premium"])
    today = series[-1]
    return {
        "sjc_peak": {"value": sjc_peak["sjc"], "date": sjc_peak["date"]},
        "sjc_today": {"value": today["sjc"]},
        "prem_peak": {"value": prem_peak["premium"], "date": prem_peak["date"]},
        "prem_today": {"value": today["premium"]},
    }


# ---------------- SVG charts ----------------

def _dmy(iso):
    y, m, d = iso.split("-")
    return f"{int(d)}/{int(m)}"


def _scale(v, vmin, vmax, h, pad):
    if vmax == vmin:
        return h / 2
    return pad + (h - 2 * pad) * (1 - (v - vmin) / (vmax - vmin))


def _xpos(i, n, w, padl, padr):
    if n <= 1:
        return padl
    return padl + (w - padl - padr) * i / (n - 1)


def chart_dual(series):
    W, H, padl, padr, padt, padb = 380, 240, 42, 12, 16, 28
    n = len(series)
    vals = [s["sjc"] for s in series] + [s["intl"] for s in series]
    vmin, vmax = min(vals) - 5, max(vals) + 5
    def pts(key):
        return " ".join(f"{_xpos(i,n,W,padl,padr):.1f},{_scale(s[key],vmin,vmax,H-padt-padb,0)+padt:.1f}"
                        for i, s in enumerate(series))
    def dots(key, color):
        return "".join(f'<circle cx="{_xpos(i,n,W,padl,padr):.1f}" cy="{_scale(s[key],vmin,vmax,H-padt-padb,0)+padt:.1f}" r="2.5" fill="{color}"/>'
                       for i, s in enumerate(series))
    # y gridlines
    grid = ""
    for k in range(5):
        gv = vmin + (vmax - vmin) * k / 4
        gy = _scale(gv, vmin, vmax, H - padt - padb, 0) + padt
        grid += f'<line x1="{padl}" y1="{gy:.1f}" x2="{W-padr}" y2="{gy:.1f}" stroke="#2a2f3a" stroke-width="0.5"/>'
        grid += f'<text x="{padl-5}" y="{gy+3:.1f}" text-anchor="end" font-size="9" fill="#7a8088">{gv:.0f}M</text>'
    # x labels (~6)
    xlab = ""
    step = max(1, n // 6)
    for i in range(0, n, step):
        x = _xpos(i, n, W, padl, padr)
        xlab += f'<text x="{x:.1f}" y="{H-8}" text-anchor="middle" font-size="9" fill="#7a8088">{_dmy(series[i]["date"])}</text>'
    return f'''<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">
      {grid}
      <polyline points="{pts('intl')}" fill="none" stroke="#5b9bd5" stroke-width="1.6" stroke-dasharray="4,3"/>
      <polyline points="{pts('sjc')}" fill="none" stroke="#5b9bd5" stroke-width="2"/>
      {dots('intl','#5b9bd5')}{dots('sjc','#5b9bd5')}
      {xlab}
    </svg>'''


def chart_premium(series):
    W, H, padl, padr, padt, padb = 380, 240, 42, 12, 16, 28
    n = len(series)
    vals = [s["premium"] for s in series]
    vmin, vmax = max(0, min(vals) - 4), max(vals) + 4
    def y(v):
        return _scale(v, vmin, vmax, H - padt - padb, 0) + padt
    line = " ".join(f"{_xpos(i,n,W,padl,padr):.1f},{y(s['premium']):.1f}" for i, s in enumerate(series))
    area = f"{padl},{y(vmin):.1f} " + line + f" {_xpos(n-1,n,W,padl,padr):.1f},{y(vmin):.1f}"
    dots = "".join(f'<circle cx="{_xpos(i,n,W,padl,padr):.1f}" cy="{y(s["premium"]):.1f}" r="2.5" fill="#7c6fd6"/>'
                   for i, s in enumerate(series))
    grid = ""
    for k in range(5):
        gv = vmin + (vmax - vmin) * k / 4
        gy = y(gv)
        grid += f'<line x1="{padl}" y1="{gy:.1f}" x2="{W-padr}" y2="{gy:.1f}" stroke="#2a2f3a" stroke-width="0.5"/>'
        grid += f'<text x="{padl-5}" y="{gy+3:.1f}" text-anchor="end" font-size="9" fill="#7a8088">{gv:.0f}M</text>'
    thr_y = y(13)
    xlab = ""
    step = max(1, n // 6)
    for i in range(0, n, step):
        x = _xpos(i, n, W, padl, padr)
        xlab += f'<text x="{x:.1f}" y="{H-8}" text-anchor="middle" font-size="9" fill="#7a8088">{_dmy(series[i]["date"])}</text>'
    return f'''<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">
      {grid}
      <polygon points="{area}" fill="#7c6fd6" fill-opacity="0.15"/>
      <polyline points="{line}" fill="none" stroke="#7c6fd6" stroke-width="2"/>
      <line x1="{padl}" y1="{thr_y:.1f}" x2="{W-padr}" y2="{thr_y:.1f}" stroke="#3fa66a" stroke-width="1.2" stroke-dasharray="5,3"/>
      {dots}{xlab}
    </svg>'''


# ---------------- HTML ----------------

def _fmt(v):
    return f"{v:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")


def render_html(series, stats, news, meta):
    now = datetime.now().strftime("%H:%M %d/%m/%Y")
    live = meta.get("live_ok")
    cur_sell = meta.get("sell") or (series[-1]["sjc"] if series else 0)
    cur_buy = meta.get("buy") or cur_sell

    def _dong(tr):  # triệu đồng/lượng -> "148.000.000"
        return f"{int(round(tr * 1e6)):,}".replace(",", ".")

    # So sánh theo ngày: delta = điểm hôm nay − điểm dữ liệu liền trước trên chart
    prev = series[-2] if len(series) >= 2 else None
    delta = round(cur_sell - prev["sjc"], 2) if prev else None
    prev_label = _dmy(prev["date"]) if prev else ""

    def _dod(today_val):
        if delta is None or delta == 0:
            return '<div class="dod muted">— không đổi so với phiên trước</div>' if delta == 0 else ""
        base = today_val - delta
        pct = (delta / base * 100) if base else 0
        up = delta > 0
        cls = "up" if up else "down"
        arrow = "▲" if up else "▼"
        sign = "+" if up else "−"
        return f'<div class="dod {cls}">{arrow} {sign}{_fmt(abs(delta))}M ({sign}{abs(pct):.2f}%) <span>so với {prev_label}</span></div>'

    price_top = f"""
    <div class="prices">
      <div class="pcard"><div class="plabel">Giá mua vào</div><div class="pval">{_dong(cur_buy)} <span>đ</span></div>{_dod(cur_buy)}</div>
      <div class="pcard"><div class="plabel">Giá bán ra</div><div class="pval">{_dong(cur_sell)} <span>đ</span></div>{_dod(cur_sell)}</div>
    </div>
    <p class="prod">VÀNG MIẾNG SJC (Vàng SJC) · đơn vị: đồng/lượng · Nguồn: BTMC (api.btmc.vn)</p>
    """

    cards = f"""
    <div class="stat"><div class="sl">SJC đỉnh ({_dmy(stats['sjc_peak']['date'])})</div><div class="sv blue">{_fmt(stats['sjc_peak']['value'])}M</div></div>
    <div class="stat"><div class="sl">SJC hôm nay</div><div class="sv blue">{_fmt(stats['sjc_today']['value'])}M</div></div>
    """ if stats else ""

    news_items = ""
    for i, a in enumerate(news, 1):
        news_items += f"""<a class="news" href="{html.escape(a['link'])}" target="_blank" rel="noopener">
          <span class="num">{i}</span><span class="ntext"><span class="ntitle">{html.escape(a['title'])}</span>
          <span class="nmeta">{html.escape(a.get('source',''))} · {html.escape(a.get('published',''))}</span></span></a>"""

    # Panel đối chiếu VnExpress
    vnx = meta.get("vnx") or {}
    xcheck = ""
    if vnx.get("sjc") or vnx.get("world"):
        def _cmp_row(label, ours, theirs, unit):
            if ours is None or theirs is None:
                return f'<tr><td class="rl">{label}</td><td>{_fmt(ours) if ours else "—"}{unit}</td><td>{_fmt(theirs) if theirs else "—"}{unit}</td><td><span class="muted">—</span></td></tr>'
            diff = theirs - ours
            pct = abs(diff) / ours * 100 if ours else 0
            ok = pct <= 1.5
            tag = f'<span class="{"ok" if ok else "warn"}">{"✓ khớp" if ok else "⚠ lệch"} {pct:.1f}%</span>'
            return f'<tr><td class="rl">{label}</td><td>{_fmt(ours)}{unit}</td><td>{_fmt(theirs)}{unit}</td><td>{tag}</td></tr>'
        sjc_ours = meta.get("sell")
        world_ours = meta.get("spot_usd")
        link = f'<a href="{html.escape(vnx["url"])}" target="_blank" rel="noopener" class="vnxlink">{html.escape(vnx.get("title") or "Xem bài VnExpress")}</a>' if vnx.get("url") else ""
        xcheck = f"""<div class="card" style="margin-top:18px">
          <h2>🔎 Đối chiếu nguồn — VnExpress</h2>
          <table class="cmp"><tr><th></th><th>Nguồn ta (BTMC/gold-api)</th><th>VnExpress</th><th>Kết quả</th></tr>
          {_cmp_row("SJC bán ra (tr/lượng)", sjc_ours, vnx.get("sjc"), "")}
          {_cmp_row("Vàng thế giới (USD/oz)", world_ours, vnx.get("world"), "")}
          </table>
          <div class="disc" style="margin:10px 0 0">Đối chiếu tự động mỗi ngày với bài giá vàng mới nhất của VnExpress. {link}</div>
        </div>"""

    src_note = ("Điểm hôm nay là số liệu LIVE thật (SJC qua BTMC, quốc tế qua gold-api.com × tỷ giá). "
                if live else "Không lấy được dữ liệu live hôm nay — hiển thị dữ liệu gần nhất. ")
    spot_txt = (f"Quốc tế quy đổi = spot {meta['spot_usd']:.1f} USD/oz × {meta['usdvnd']:.0f} VNĐ/USD × {OZ_PER_LUONG}. "
                if live else f"Quốc tế quy đổi = spot USD/oz × ~25.500-26.400 VNĐ/USD × {OZ_PER_LUONG}. ")

    return f"""<!DOCTYPE html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Giá Vàng Hôm Nay · {now}</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e7e9ee;margin:0;padding:24px}}
.wrap{{max-width:900px;margin:0 auto}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#9aa0ad;font-size:13px;margin-bottom:20px}}
.prices{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:6px}}
.pcard{{flex:1;min-width:220px;background:#171a21;border:1px solid #242832;border-radius:12px;padding:16px 18px}}
.plabel{{color:#9aa0ad;font-size:12px;text-transform:uppercase;letter-spacing:.5px}}
.pval{{font-size:30px;font-weight:700;color:#f5c542;margin-top:6px}}
.pval span{{font-size:15px;color:#9aa0ad;font-weight:400}}
.prod{{color:#9aa0ad;font-size:12px;margin:0 0 18px}}
.dod{{font-size:13px;font-weight:600;margin-top:8px}}
.dod span{{color:#7a8088;font-weight:400;font-size:11px}}
.dod.up{{color:#3fa66a}} .dod.down{{color:#f87171}} .dod.muted{{color:#7a8088;font-weight:400}}
.charts{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
@media(max-width:720px){{.charts{{grid-template-columns:1fr}}}}
.card{{background:#171a21;border:1px solid #242832;border-radius:14px;padding:18px}}
.ctitle{{font-size:14px;font-weight:600;margin:0 0 4px}}
.legend{{font-size:11px;color:#9aa0ad;margin-bottom:8px}}
.legend b{{font-weight:600}} .lg-red{{color:#5b9bd5}} .lg-blue{{color:#5b9bd5}} .lg-purple{{color:#7c6fd6}} .lg-green{{color:#3fa66a}}
.axis{{font-size:10px;color:#6b7280;text-align:center;margin-top:2px}}
.chartnote{{font-size:11px;color:#7a8088;margin-top:8px;line-height:1.4}}
.stats{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin:18px 0}}
.stat{{background:#171a21;border:1px solid #242832;border-radius:12px;padding:14px}}
.sl{{color:#9aa0ad;font-size:12px;margin-bottom:6px}} .sv{{font-size:22px;font-weight:700;color:#e7e9ee}}
.sv.blue{{color:#5b9bd5}} .sv.green{{color:#3fa66a}}
.disc{{color:#6b7280;font-size:11px;line-height:1.5;margin:8px 0 20px}}
h2{{font-size:15px;margin:0 0 12px;color:#cdd2db}}
table.cmp{{width:100%;border-collapse:collapse}}
table.cmp th,table.cmp td{{padding:9px 8px;text-align:right;border-bottom:1px solid #242832;font-size:13px}}
table.cmp th{{color:#9aa0ad;font-weight:500;font-size:11px}}
td.rl{{text-align:left}}
.ok{{color:#3fa66a}} .warn{{color:#e0a13d}} .muted{{color:#6b7280}}
.vnxlink{{color:#5b9bd5;text-decoration:none}} .vnxlink:hover{{text-decoration:underline}}
.calc{{margin-top:10px}}
.clabel{{display:block;font-size:12px;color:#9aa0ad;margin-bottom:8px}}
.inrow{{display:flex;gap:8px;margin-bottom:14px}}
.inrow input{{flex:1;background:#0f1115;border:1px solid #2a2f3a;border-radius:8px;color:#e7e9ee;padding:10px 12px;font-size:15px;outline:none}}
.inrow input:focus{{border-color:#5b9bd5}}
.inrow button{{background:#f5c542;color:#0f1115;border:none;border-radius:8px;padding:0 18px;font-weight:700;font-size:14px;cursor:pointer}}
.inrow button:hover{{background:#e3b52f}}
.crow{{display:flex;justify-content:space-between;font-size:13px;padding:7px 0;border-bottom:1px solid #242832;color:#cdd2db}}
.crow b{{color:#e7e9ee}}
.cres{{margin-top:12px;padding:12px;border-radius:8px;font-size:15px;font-weight:700;text-align:center;background:#1e2430}}
.cres.ok{{color:#3fa66a}} .cres.warn{{color:#f87171}}
.calcout{{min-height:20px}}
.news{{display:flex;gap:12px;align-items:flex-start;text-decoration:none;color:inherit;padding:12px;border-radius:10px}}
.news:hover{{background:#1e2430}}
.num{{background:#f5c542;color:#0f1115;font-weight:700;border-radius:50%;width:24px;height:24px;display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0}}
.ntitle{{display:block;font-size:14px;line-height:1.4}} .nmeta{{display:block;color:#9aa0ad;font-size:12px;margin-top:4px}}
</style></head><body><div class="wrap">
<h1>🏆 Giá Vàng Hôm Nay</h1><div class="sub">Cập nhật lúc {now}</div>
{price_top}
<div class="charts">
  <div class="card"><div class="ctitle">Giá vàng trong nước vs quốc tế <span style="font-size:10px;color:#7a8088;font-weight:400">· dữ liệu minh hoạ</span></div>
    <div class="legend"><b class="lg-red">━ SJC bán ra</b>  <b class="lg-blue">┅ Quốc tế quy đổi</b></div>
    {chart_dual(series)}<div class="axis">Triệu đồng/lượng</div>
    <div class="chartnote">Ghi chú: Giá quốc tế quy đổi = spot XAU (USD/oz) × tỷ giá USD/VND × 1,2057 (oz/lượng)</div></div>
  <div class="card"><div class="ctitle">So sánh giá vàng bạn đã mua</div>
    <div class="legend">Nhập giá bạn đã mua để tính lãi/lỗ so với giá hiện tại</div>
    <div class="calc">
      <label class="clabel">Giá bạn đã mua (triệu đồng / lượng)</label>
      <div class="inrow">
        <input id="buyprice" type="number" step="0.1" min="0" placeholder="VD: 145.0" oninput="calcGold()">
        <button type="button" onclick="calcGold()">So sánh</button>
      </div>
      <div id="calcout" class="calcout"><span class="muted">Nhập giá để xem kết quả…</span></div>
    </div>
  </div>
</div>
<div class="disc">{src_note}{spot_txt}Các điểm trước hôm nay là ước tính nội suy từ diễn biến thị trường, sẽ được thay bằng số liệu thật khi agent chạy mỗi ngày. Hover để xem giá trị tại từng điểm.</div>
{xcheck}
<div class="card" style="margin-top:18px"><h2>📰 Tin tức nổi bật trong ngày</h2>{news_items}</div>
<div class="axis" style="margin-top:16px">🪙 Gold Companion · Powered by GreenNode AgentBase · <b style="color:#f5c542">Developed by Yuna</b></div>
</div>
<script>
var CUR_BUY={cur_buy}, CUR_SELL={cur_sell};
function fmt(v){{return v.toLocaleString('vi-VN',{{minimumFractionDigits:1,maximumFractionDigits:2}});}}
function calcGold(){{
  var el=document.getElementById('buyprice'), out=document.getElementById('calcout');
  var p=parseFloat(el.value);
  if(isNaN(p)||p<=0){{out.innerHTML='<span class="muted">Nhập giá để xem kết quả…</span>';return;}}
  var realized=CUR_BUY-p, pct=p>0?(realized/p*100):0;
  var profit=realized>=0;
  var cls=profit?'ok':'warn', arrow=profit?'▲':'▼', word=profit?'Lãi':'Lỗ';
  out.innerHTML=
    '<div class="crow"><span>Giá bán ra hiện tại (thị giá)</span><b>'+fmt(CUR_SELL)+'M</b></div>'+
    '<div class="crow"><span>Giá mua vào hiện tại (tiệm thu lại)</span><b>'+fmt(CUR_BUY)+'M</b></div>'+
    '<div class="crow"><span>Giá bạn đã mua</span><b>'+fmt(p)+'M</b></div>'+
    '<div class="cres '+cls+'">'+arrow+' '+word+' nếu bán lại: '+fmt(Math.abs(realized))+'M/lượng ('+(profit?'+':'-')+Math.abs(pct).toFixed(2)+'%)</div>';
}}
</script>
</body></html>"""


# ---------------- Entrypoint ----------------

def _build_all():
    series, meta = build_series()
    stats = compute_stats(series)
    meta["vnx"] = fetch_vnexpress()
    news = fetch_gold_news()
    page = render_html(series, stats, news, meta)
    return series, stats, news, meta, page


def _summary_from(holdings: list) -> dict:
    prices = fetch_prices_map()
    summ = compute_portfolio(holdings, prices)
    summ["prices"] = prices
    summ["holdings_raw"] = holdings
    summ["asof"] = datetime.now().strftime("%H:%M %d/%m/%Y")
    # Daily movement: biến động tài sản hôm nay = tổng lượng × (giá SJC mua-vào hôm nay − hôm qua)
    # Dùng cùng nguồn giá single-row với price-history để Δ chính xác (0 nếu giá không đổi).
    summ["totals"]["day_change"] = None
    summ["sjc_change_pct"] = None
    try:
        sjc = fetch_sjc_today()
        sjc_today = sjc[0] if sjc else None
        if sjc_today:
            hist = mem_browse_history()
            today = datetime.now().date().isoformat()
            prev = None
            for d in sorted(hist.keys()):
                if d < today:
                    prev = hist[d]
            if prev and prev.get("buy"):
                delta = sjc_today - prev["buy"]
                summ["sjc_change_pct"] = round(delta / prev["buy"] * 100, 2)
                qty = summ["totals"].get("qty", 0)
                if qty:
                    summ["totals"]["day_change"] = round(qty * delta, 3)
            # Tự ghi giá hôm nay vào Memory nếu chưa có — kích hoạt khi BẤT KỲ ai truy cập (kể cả người vote)
            if today not in hist:
                intl, _, _ = fetch_intl_today()
                if intl is not None and sjc:
                    mem_insert_today(today, sjc[0], sjc[1], intl)
    except Exception:
        pass
    return summ


def _portfolio_summary(key: str) -> dict:
    # Dùng cho đọc (pf_list): tải từ Memory theo khóa lưu trữ (uid hoặc tên)
    return _summary_from(pf_load(key))


def _next_id(holdings: list) -> str:
    n = 0
    for h in holdings:
        try:
            n = max(n, int(h.get("id", 0)))
        except Exception:
            pass
    return str(n + 1)


@app.entrypoint
def handler(payload: dict, context: RequestContext) -> dict:
    action = (payload.get("action") or "").lower()
    user = payload.get("user") or (getattr(context, "user_id", None) or "guest")
    # Khóa lưu trữ danh mục = uid theo trình duyệt (tránh trùng tên → đè portfolio của nhau).
    # Fallback về tên nếu client cũ chưa gửi uid. `user` (tên) vẫn dùng để cá nhân hóa AI.
    pf_key = payload.get("uid") or user

    # ----- Portfolio actions (Gold Wealth Companion) -----
    if action == "pf_list":
        return {"status": "success", "ai_disclosure": True, **_portfolio_summary(pf_key)}

    if action == "pf_add":
        h = payload.get("holding") or {}
        # Backstop đơn vị: giá vàng thực tế ~[10,1000] triệu/lượng. Tự chuẩn hóa nếu
        # client nhầm nhập nguyên giá đồng/lượng (vd 150000000), chặn giá phi lý vào Memory.
        try:
            bp = float(h.get("buy_price", 0) or 0)
        except (TypeError, ValueError):
            bp = 0.0
        for d in (1, 1e6, 1e3):
            if 10 <= bp / d <= 1000:
                bp = round(bp / d, 3)
                break
        if not (10 <= bp <= 1000):
            return {"status": "error", "message": "Giá mua không hợp lệ (cần theo triệu đồng/lượng, vd 150)."}
        holdings = pf_load(pf_key)
        h["id"] = _next_id(holdings)
        holdings.append({
            "id": h["id"], "type": h.get("type", "sjc"),
            "qty": float(h.get("qty", 0) or 0), "buy_price": bp,
            "buy_date": h.get("buy_date", ""),
        })
        pf_save(pf_key, holdings)
        return {"status": "success", **_summary_from(holdings)}

    if action == "pf_delete":
        hid = str(payload.get("id", ""))
        holdings = [h for h in pf_load(pf_key) if str(h.get("id")) != hid]
        pf_save(pf_key, holdings)
        return {"status": "success", **_summary_from(holdings)}

    if action == "pf_clear":
        pf_save(pf_key, [])
        return {"status": "success", **_summary_from([])}

    if action == "pf_sell":
        typ = payload.get("type", "")
        sell_qty = float(payload.get("qty", 0) or 0)
        out, remain = [], sell_qty
        for h in pf_load(pf_key):
            if h.get("type") == typ and remain > 0:
                hq = float(h.get("qty", 0))
                if hq <= remain + 1e-9:
                    remain -= hq
                    continue  # bán hết lệnh này
                h = dict(h)
                h["qty"] = round(hq - remain, 4)
                remain = 0
            out.append(h)
        pf_save(pf_key, out)
        return {"status": "success", **_summary_from(out)}

    if action == "pf_seed_demo":
        demo = [
            {"id": "1", "type": "sjc", "qty": 2, "buy_price": 142.0, "buy_date": "2026-03-01"},
            {"id": "2", "type": "nhan", "qty": 1, "buy_price": 139.0, "buy_date": "2026-04-20"},
            {"id": "3", "type": "nutrang", "qty": 1, "buy_price": 148.0, "buy_date": "2026-05-20"},
        ]
        pf_save(pf_key, demo)
        return {"status": "success", **_summary_from(demo)}

    if action == "market_data":
        return {"status": "success", **build_market_data()}

    if action in ("ai_insight", "chat"):
        raw = payload.get("holdings")
        summ = _summary_from(raw) if isinstance(raw, list) else _portfolio_summary(pf_key)
        q = payload.get("message", "") if action == "chat" else ""
        uname = str(user) if (user and str(user).lower() != "guest") else "bạn"
        arts = fetch_gold_news_diverse(3)
        news = [f"{a.get('source','')}: {a.get('title','')}".strip(": ") for a in arts]
        return {"status": "success", "ai_disclosure": True,
                "reply": _bold_html(ai_insight(summ, q, user_name=uname, news=news)),
                "totals": summ["totals"], "news": news, "news_items": arts,
                "timestamp": datetime.now().isoformat()}

    # ----- Market dashboard (mặc định, giữ tương thích) -----
    fmt = (payload.get("format") or "json").lower()
    series, stats, news, meta, page = _build_all()
    if fmt == "html":
        return {"status": "success", "html": page, "timestamp": datetime.now().isoformat()}
    return {"status": "success", "meta": meta, "stats": stats, "series": series,
            "news": news, "html": page, "timestamp": datetime.now().isoformat()}


@app.ping
def health_check() -> PingStatus:
    return PingStatus.HEALTHY


COMPANION_HTML = """<!DOCTYPE html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gold Companion — Người bạn đồng hành tài sản vàng</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e7e9ee;margin:0;padding:20px}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:22px;margin:0 0 2px}
.sub{color:#9aa0ad;font-size:13px;margin-bottom:14px}
.ai-banner{background:#1e2430;border:1px solid #2f3a4a;border-radius:10px;padding:9px 12px;font-size:12px;color:#9aa0ad;margin-bottom:16px}
.ai-banner b{color:#f5c542}
.card{background:#171a21;border:1px solid #242832;border-radius:14px;padding:18px;margin-bottom:16px}
.userrow{display:flex;gap:8px;align-items:center;margin-bottom:16px}
.userrow input{flex:1;background:#0f1115;border:1px solid #2a2f3a;border-radius:8px;color:#e7e9ee;padding:9px 12px;font-size:14px;outline:none}
.userrow input:focus{border-color:#5b9bd5}
.namehint{font-size:11px;color:#7a8088;margin:-6px 0 14px;line-height:1.5}
.btn{background:#f5c542;color:#0f1115;border:none;border-radius:8px;padding:9px 16px;font-weight:700;font-size:14px;cursor:pointer}
.btn:hover{background:#e3b52f}
.btn.ghost{background:transparent;border:1px solid #2f3a4a;color:#cdd2db}
.btn.ghost:hover{background:#1e2430}
.btn.ghost.danger{color:#f87171;border-color:#3a2326}
.btn.ghost.danger:hover{background:#251416}
.pf-actions{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.pf-actions .danger{margin-left:auto}
.hero{background:#15171e;border:1px solid #2a2f3a;border-radius:20px;padding:26px 22px;margin-bottom:14px;text-align:center}
.hlabel{color:#9aa0ad;font-size:13px;margin-bottom:6px}
.hasset{font-size:17px;color:#cdd2db;font-weight:600;margin-bottom:10px}
.pnlbig{font-size:46px;font-weight:800;line-height:1.05;letter-spacing:-1px;margin-bottom:6px}
.pnlcap{font-size:12px;color:#7a8088;margin-bottom:14px}
.chips{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-bottom:18px}
.chip{background:#1e2430;border:1px solid #2f3a4a;border-radius:999px;padding:7px 14px;font-size:13px;font-weight:600;color:#cdd2db}
.chip.pnl-pos{background:#13251a;border-color:#2e5a3a;color:#4ade80}
.chip.pnl-neg{background:#251416;border-color:#5a2e32;color:#f87171}
.hinsight{background:#1c2029;border-left:3px solid #f5c542;border-radius:10px;padding:13px 15px;font-size:14px;line-height:1.55;text-align:left;min-height:22px;color:#e7e9ee}
.hinsight .who{color:#f5c542;font-weight:700;font-size:12px;display:block;margin-bottom:3px}
.aitag{font-size:11px;color:#f5c542;border:1px solid #3a3520;background:#1c1a12;border-radius:999px;padding:3px 9px}
.top{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.brand{font-size:18px;font-weight:800}
.userbox{display:flex;gap:6px;align-items:center}
.userbox input{width:160px;background:#0f1115;border:1px solid #2a2f3a;border-radius:8px;color:#e7e9ee;padding:7px 10px;font-size:12px;outline:none}
.userbox input:focus{border-color:#5b9bd5}
.btn.sm{padding:7px 12px;font-size:12px}
.profile-chip{background:#1c1a12;border:1px solid #3a3520;color:#f5c542;border-radius:999px;padding:6px 14px;font-size:13px;font-weight:600;cursor:pointer;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.profile-chip:hover{background:#24210f}
.disc{font-size:11px;color:#6b7280;text-align:center;margin-bottom:8px}
.flowbar{font-size:11px;color:#9aa0ad;text-align:center;margin-bottom:14px;background:#15171e;border:1px solid #242832;border-radius:999px;padding:7px 10px;line-height:1.7}
.flowbar span{color:#5b6270;margin:0 2px} .flowbar b{color:#0a84ff}
.btn.zalo{background:#0068ff;color:#fff} .btn.zalo:hover{background:#0057d6}
.ctazone{margin-top:12px;display:flex;flex-direction:column;align-items:center;gap:6px}
.btn.act-gold{width:100%;background:linear-gradient(135deg,#f7d774,#f5c542);color:#0f1115;border:none;border-radius:12px;padding:12px;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:1px;box-shadow:0 6px 20px rgba(245,197,66,.22)}
.btn.act-gold:hover{filter:brightness(1.04)}
.ag-main{font-size:15px;font-weight:800;letter-spacing:.2px}
.ag-sub{font-size:11px;font-weight:500;opacity:.65}
.modal-tabs{display:flex;gap:6px;margin-bottom:14px}
.mtab{flex:1;background:#0f1115;border:1px solid #2a2f3a;border-radius:8px;color:#9aa0ad;padding:9px;font-size:13px;font-weight:600;cursor:pointer}
.mtab.active{background:#f5c542;color:#0f1115;border-color:#f5c542}
.ask-again{background:none;border:none;color:#7a8088;font-size:12px;cursor:pointer;text-decoration:underline}
.ask-again:hover{color:#cdd2db}
.news-acc{margin-top:14px;border-top:1px solid #242832;padding-top:8px;opacity:.6}
.news-acc:hover{opacity:1}
.news-acc summary{font-size:12px;color:#9aa0ad;font-weight:600;cursor:pointer;list-style:none}
.news-acc summary::-webkit-details-marker{display:none}
.news-acc summary:before{content:"▸ ";color:#5b6270}
.news-acc[open] summary:before{content:"▾ "}
.btn:disabled{opacity:.4;cursor:not-allowed}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:50;align-items:center;justify-content:center;padding:18px}
.modal.open{display:flex}
.modal-box{background:#171a21;border:1px solid #2a2f3a;border-radius:16px;padding:22px;max-width:380px;width:100%}
.modal-h{font-size:16px;font-weight:700;margin-bottom:14px}
.modal-box .ml{display:block;font-size:12px;color:#9aa0ad;margin:10px 0 4px}
.modal-box select,.modal-box input{width:100%;background:#0f1115;border:1px solid #2a2f3a;border-radius:8px;color:#e7e9ee;padding:10px;font-size:14px}
.buy-sum{background:#1e2430;border-radius:10px;padding:12px;margin:14px 0;font-size:13px;line-height:1.7}
.modal-act{display:flex;gap:10px} .modal-act .btn{flex:1}
.giadinh{color:#7a8088;font-size:11px}
.demo-badge{background:#3a3520;color:#f5c542;font-size:10px;font-weight:700;border-radius:4px;padding:2px 6px;vertical-align:middle;letter-spacing:.5px}
#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%) translateY(20px);max-width:88%;background:#15171e;border:1px solid #3a3520;border-left:3px solid #f5c542;border-radius:12px;padding:13px 16px;font-size:13px;color:#e7e9ee;line-height:1.5;box-shadow:0 14px 40px rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;z-index:9999}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#toast b{color:#f5c542}
.login-box{max-width:400px;text-align:center}
.login-logo{font-size:42px;line-height:1;margin-bottom:8px}
.login-title{font-size:23px;font-weight:800;letter-spacing:.3px}
.login-sub{font-size:13px;color:#9aa0ad;margin-bottom:18px}.login-sub b{color:#f5c542}
.login-benefits{text-align:left;background:#0f1115;border:1px solid #242832;border-radius:12px;padding:14px;margin-bottom:18px;display:flex;flex-direction:column;gap:11px}
.lb{font-size:13px;color:#cdd2db;display:flex;gap:10px;align-items:flex-start;line-height:1.45}
.lb span{font-size:17px;flex-shrink:0}.lb b{color:#f5c542}
.login-box .ml{display:block;text-align:left;font-size:12px;color:#9aa0ad;margin-bottom:6px}
.login-box input{width:100%;background:#0f1115;border:1px solid #2a2f3a;border-radius:9px;color:#e7e9ee;padding:11px 12px;font-size:15px;outline:none}
.login-box input:focus{border-color:#f5c542}
.login-cta{width:100%;margin-top:14px;padding:13px;font-size:15px;font-weight:700}
.login-foot{font-size:11px;color:#6b7280;margin-top:12px;line-height:1.4}
.skip-link{background:none;border:none;color:#7a8088;font-size:12px;text-decoration:underline;cursor:pointer;margin-top:10px}
.skip-link:hover{color:#cdd2db}
.mktcard{background:#171a21;border:1px solid #242832;border-radius:12px;padding:11px 16px;margin-bottom:14px;font-size:13px;color:#cdd2db}
.mktcard b{color:#f5c542}
.mktcard .mk-sub{color:#7a8088;font-size:11px}
.mk-badge{font-size:11px;font-weight:700;border-radius:999px;padding:2px 8px;margin-left:4px}
.mk-badge.up{color:#4ade80;background:#13251a} .mk-badge.down{color:#f87171;background:#251416} .mk-badge.flat{color:#9aa0ad;background:#1e2430}
.inrow input:disabled,.chatrow input:disabled{opacity:.5;cursor:not-allowed}
.btn:disabled{opacity:.4;cursor:not-allowed}
.pnl-pos{color:#4ade80} .pnl-neg{color:#f87171} .pnl-flat{color:#cdd2db}
h2{font-size:14px;margin:0 0 12px;color:#cdd2db}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:430px}
th,td{padding:9px 6px;text-align:right;border-bottom:1px solid #242832;white-space:nowrap}
th{color:#9aa0ad;font-weight:500;font-size:11px}
td.l,th.l{text-align:left}
.lot-date{font-size:11px;color:#7a8088;font-weight:400;margin-top:2px}
.del{background:none;border:none;color:#f87171;cursor:pointer;font-size:15px;line-height:1;padding:4px 6px}
.qprompts{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.qp{background:#1e2430;border:1px solid #2f3a4a;border-radius:999px;padding:6px 12px;font-size:12px;color:#cdd2db;cursor:pointer;text-align:left}
.qp:hover{background:#242c3a;border-color:#3a4a5e}
.newsbox{margin-top:14px}
.nb-h{font-size:12px;color:#9aa0ad;margin-bottom:4px;font-weight:600}
.nb-i{display:block;font-size:12px;color:#5b9bd5;text-decoration:none;padding:7px 0;border-top:1px solid #242832;line-height:1.45}
.nb-i:hover{text-decoration:underline}
@media(max-width:560px){
  body{padding:14px}
  .pnlbig{font-size:38px}
  .tablewrap{overflow:visible}
  table{min-width:0}
  thead{display:none}
  tbody tr{display:block;border:1px solid #2a2f3a;border-radius:10px;padding:8px 12px;margin-bottom:10px}
  tbody td{display:flex;justify-content:space-between;align-items:center;border:none;padding:5px 0;text-align:right;white-space:normal}
  tbody td::before{content:attr(data-label);color:#9aa0ad;font-size:12px;font-weight:500;text-align:left;margin-right:12px}
  tbody td.l{font-size:15px;font-weight:700;border-bottom:1px solid #242832;padding-bottom:8px;margin-bottom:4px}
  tbody td.l::before,tbody td.act::before{content:''}
  tbody td.act{justify-content:flex-end}
  .pf-actions .btn{flex:1 1 0;min-width:0;white-space:nowrap}
  .pf-actions .danger{flex:1 0 100%;margin-left:0;margin-top:2px}
}
.form-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:6px}
.form-grid label{display:block;font-size:11px;color:#9aa0ad;margin-bottom:4px}
.form-grid input,.form-grid select{width:100%;background:#0f1115;border:1px solid #2a2f3a;border-radius:8px;color:#e7e9ee;padding:9px;font-size:14px}
.del{background:none;border:none;color:#f87171;cursor:pointer;font-size:13px}
.insight{background:#1e2430;border-left:3px solid #f5c542;border-radius:8px;padding:14px;font-size:14px;line-height:1.55;min-height:24px}
.chatrow{display:flex;gap:8px;margin-top:12px}
.chatrow input{flex:1;background:#0f1115;border:1px solid #2a2f3a;border-radius:8px;color:#e7e9ee;padding:10px 12px;font-size:14px;outline:none}
.muted{color:#7a8088}
.spin{display:inline-block;width:14px;height:14px;border:2px solid #3a3f4a;border-top-color:#f5c542;border-radius:50%;animation:s 0.8s linear infinite;vertical-align:middle}
@keyframes s{to{transform:rotate(360deg)}}
a.market{color:#5b9bd5;font-size:13px;text-decoration:none}
.foot{color:#6b7280;font-size:12px;text-align:center;margin-top:8px}
.foot b{color:#f5c542}
</style></head><body><div class="wrap">
<div class="top">
  <div class="brand">🪙 Aurum</div>
  <button class="profile-chip" onclick="openName()" id="profileChip">👤 Nhập tên</button>
</div>
<div class="disc">Bạn đang trò chuyện với <b>Aurum</b> — trợ lý <b>AI</b> · thông tin tham khảo, không phải lời khuyên đầu tư</div>

<div class="hero" id="heroCard">
  <div class="hlabel" id="hlabel">💰 Tổng tài sản vàng của bạn</div>
  <div class="hasset" id="totVal">—</div>
  <div class="pnlbig" id="totPnl">—</div>
  <div class="pnlcap">Tổng lời / lỗ so với vốn bỏ ra</div>
  <div class="chips">
    <span class="chip" id="roiChip">ROI —</span>
  </div>
  <div class="hinsight" id="insight"><span class="who">🤖 Aurum</span><span class="muted">Đang theo dõi tài sản của bạn…</span></div>
  <div class="ctazone" id="ctazone"></div>
  <div class="qprompts" id="qprompts"></div>
  <div class="chatrow" id="chatrow">
    <input id="chatmsg" placeholder="Nhập câu hỏi khác cho Aurum…" onkeydown="if(event.key==='Enter')sendChat()"/>
    <button class="btn" id="askBtn" onclick="sendChat()">Hỏi</button>
  </div>
  <div class="newsbox" id="newsbox"></div>
</div>

<div class="mktcard" id="mkt">📈 Đang tải giá thị trường…</div>

<div class="card" id="pfCard">
  <h2>📊 Danh mục vàng của bạn</h2>
  <div class="tablewrap"><table id="pfTable"><thead><tr>
    <th class="l">Loại</th><th>SL</th><th>Giá mua</th><th>Giá TK</th><th>Lời/Lỗ</th><th>ROI</th><th></th>
  </tr></thead><tbody id="pfBody"><tr><td colspan="7" class="muted l">Chưa có dữ liệu</td></tr></tbody></table></div>
  <div class="muted" style="font-size:11px;margin-top:8px">Giá thanh khoản = giá tiệm mua lại (số tiền bạn thực nhận khi bán).</div>
  <div class="pf-actions"><button class="btn" onclick="openAdd()">+ Thêm tài sản</button><button class="btn ghost" onclick="seedDemo()">Dùng dữ liệu mẫu</button><button class="btn ghost danger" id="clearBtn" onclick="clearPortfolio()">Xóa danh mục</button></div>
</div>

<div class="modal" id="addModal"><div class="modal-box">
  <div class="modal-h">➕ Thêm tài sản vàng</div>
  <div class="form-grid">
    <div><label class="ml">Loại vàng</label><select id="f_type">
      <option value="sjc">Vàng miếng SJC</option>
      <option value="nhan">Nhẫn tròn trơn</option>
      <option value="nutrang">Vàng nữ trang</option>
    </select></div>
    <div><label class="ml">Số lượng (lượng)</label><input id="f_qty" type="number" step="0.1" placeholder="VD: 2"/></div>
    <div><label class="ml">Giá mua (triệu đồng/lượng)</label><input id="f_price" type="number" step="0.1" placeholder="VD: 150"/><div class="giadinh" style="margin-top:5px">Nhập theo <b>triệu/lượng</b> — VD 150 triệu/lượng nhập <b>150</b>.</div></div>
    <div><label class="ml">Ngày mua (không bắt buộc)</label><input id="f_date" type="text" inputmode="numeric" placeholder="dd/mm/yyyy"/></div>
  </div>
  <div class="modal-act" style="margin-top:16px"><button class="btn ghost" onclick="closeAdd()">Hủy</button><button class="btn" onclick="confirmAdd()">Thêm vào danh mục</button></div>
</div></div>

<div class="modal" id="buyModal">
  <div class="modal-box">
    <div class="modal-h">Giao dịch vàng qua Zalopay <span class="demo-badge">DEMO</span></div>
    <div class="modal-tabs"><button id="tabBuy" class="mtab active" onclick="setMode('buy')">Mua thêm</button><button id="tabSell" class="mtab" onclick="setMode('sell')">Bán ra</button></div>
    <label class="ml">Loại vàng</label>
    <select id="b_type" onchange="onTypeChange()">
      <option value="sjc">Vàng miếng SJC</option>
      <option value="nhan">Nhẫn tròn trơn</option>
      <option value="nutrang">Vàng nữ trang</option>
    </select>
    <label class="ml">Số lượng (lượng)</label>
    <input id="b_qty" type="number" step="0.1" min="0.1" value="0.1" oninput="updModal()"/>
    <div class="buy-sum" id="b_sum"></div>
    <div class="modal-act"><button class="btn ghost" onclick="closeBuy()">Hủy</button><button class="btn" id="txnBtn" onclick="confirmTxn()">Thanh toán</button></div>
    <div class="giadinh" style="margin-top:10px">⚠️ Giao dịch vàng qua Zalopay là tính năng <b>giả định</b> cho cuộc thi — không phát sinh thanh toán thật.</div>
  </div>
</div>

<div class="modal" id="nameModal"><div class="modal-box login-box">
  <div class="login-logo">🪙</div>
  <div class="login-title">Gold Companion</div>
  <div class="login-sub">Biết vàng của bạn đang <b>lời hay lỗ</b> — mỗi ngày</div>
  <div class="login-benefits">
    <div class="lb"><span>📊</span><div>Theo dõi <b>lời / lỗ &amp; ROI</b> danh mục vàng realtime — không chỉ xem giá thị trường.</div></div>
    <div class="lb"><span>🤖</span><div>Aurum <b>tư vấn cá nhân hóa</b> theo danh mục &amp; tin tức trong ngày.</div></div>
    <div class="lb"><span>💾</span><div>Lưu danh mục <b>theo tên</b> — Aurum ghi nhớ qua mỗi lần bạn quay lại.</div></div>
  </div>
  <label class="ml">Nhập tên của bạn để bắt đầu</label>
  <input id="user" placeholder="Vd: Minh, Chi, Tùng…" onkeydown="if(event.key==='Enter')saveUser()"/>
  <button class="btn login-cta" onclick="saveUser()">Bắt đầu theo dõi tài sản →</button>
  <div class="login-foot">Chỉ cần tên · số liệu demo, không thu thập dữ liệu cá nhân thật</div>
  <button class="skip-link" onclick="closeName()">Bỏ qua, xem thử trước</button>
</div></div>

<div class="foot">🪙 Gold Companion · Powered by GreenNode AgentBase · <b>Developed by Yuna</b></div>
</div>
<div id="toast"></div>
<script>
function fmt(v){return (v||0).toLocaleString('vi-VN',{minimumFractionDigits:1,maximumFractionDigits:2});}
function fmtDate(s){if(!s)return '';var p=String(s).split('-');return p.length===3?(p[2]+'/'+p[1]+'/'+p[0]):s;}
function getUser(){return localStorage.getItem('gc_user')||'';}
function openName(){document.getElementById('user').value=getUser();document.getElementById('nameModal').classList.add('open');document.getElementById('user').focus();}
function closeName(){document.getElementById('nameModal').classList.remove('open');}
function saveUser(){var u=document.getElementById('user').value.trim();if(u){localStorage.setItem('gc_user',u);closeName();load();}}
function updProfileChip(){var u=getUser(),el=document.getElementById('profileChip');if(el)el.textContent=u?('👤 '+u):'👤 Nhập tên';}
function getUid(){var u=localStorage.getItem('gc_uid');if(!u){u=(window.crypto&&crypto.randomUUID)?crypto.randomUUID():('u-'+Date.now().toString(16)+'-'+Math.random().toString(16).slice(2,10));localStorage.setItem('gc_uid',u);}return u;}
async function api(body){body.user=getUser()||'guest';body.uid=getUid();const r=await fetch('/invocations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return r.json();}
function cls(v){return v>0?'pnl-pos':(v<0?'pnl-neg':'pnl-flat');}
function pct(v,d){return Math.abs(v||0).toFixed(d).replace('.',',');}
function setIns(html){document.getElementById('insight').innerHTML='<span class="who">🤖 Aurum</span>'+html;}
var _toastT=null;
function showToast(html){var el=document.getElementById('toast');if(!el)return;el.innerHTML=html;el.classList.add('show');if(_toastT)clearTimeout(_toastT);_toastT=setTimeout(function(){el.classList.remove('show');},4200);}
// Lớp 1: tự nhận diện đơn vị giá mua. Giá vàng thực tế ~[10,1000] triệu/lượng.
// User hay nhập nguyên giá đồng/lượng (150.000.000) → tự chuẩn hóa về triệu/lượng.
function normPrice(raw){
  var v=parseFloat(String(raw==null?'':raw).replace(',','.'))||0;
  if(v<=0)return {v:0,note:''};
  var divs=[[1,''],[1e6,'đồng/lượng'],[1e3,'nghìn đồng/lượng']];
  for(var i=0;i<divs.length;i++){var nv=Math.round(v/divs[i][0]*1000)/1000;
    if(nv>=10&&nv<=1000)return {v:nv,note:i?('🤖 <b>Aurum:</b> Đã hiểu bạn nhập theo '+divs[i][1]+' → chuẩn hóa thành <b>'+fmt(nv)+' triệu/lượng</b>.'):''};}
  return {v:v,note:''}; // ngoài mọi thang → rào cứng bên dưới chặn
}
function render(d){
  window.lastHoldings=d.holdings_raw||[];
  window.lastPrices=d.prices||window.lastPrices||{};
  var t=d.totals||{}, rows=d.rows||[];
  window.lastRows=rows;
  var u=getUser(),hl=document.getElementById('hlabel');
  if(hl)hl.textContent=(u&&rows.length)?('💰 Danh mục tài sản của '+u):'💰 Tổng tài sản vàng của bạn';
  updProfileChip();
  var sj=(d.prices||{}).sjc||{};
  if(sj.buy){
    var chg=d.sjc_change_pct, badge='';
    if(chg!==null&&chg!==undefined){var bc=chg>0?'up':(chg<0?'down':'flat'),ar=chg>0?'▲ +':(chg<0?'▼ −':'■ ');badge=' <span class="mk-badge '+bc+'">'+ar+pct(chg,2)+'%</span>';}
    var when=d.asof?('<span class="mk-sub"> · '+d.asof+'</span>'):'';
    document.getElementById('mkt').innerHTML='📈 <b>SJC thị trường</b>'+badge+when+'<br>Mua vào <b>'+fmt(sj.buy)+'M</b> · Bán ra <b>'+fmt(sj.sell)+'M</b><br><span class="mk-sub">🔄 làm mới sau <span id="mktCountdown">'+(window.refreshLeft||60)+'</span>s</span> · <a class="market" href="/market" target="_blank">📈 Xem biểu đồ →</a>';
  }
  document.getElementById('totVal').textContent='Tổng '+fmt(t.value)+'M';
  var pe=document.getElementById('totPnl');
  var sg=(t.pnl>0?'+':(t.pnl<0?'−':''));
  pe.textContent=sg+fmt(Math.abs(t.pnl))+'M';
  pe.className='pnlbig '+cls(t.pnl);
  var rc=document.getElementById('roiChip');
  rc.textContent='ROI '+sg+pct(t.roi,2)+'%';rc.className='chip '+cls(t.pnl);
  // Empty state: làm mờ & khóa ô chat + quick prompts; chỉ bật khi đã có danh mục
  var ci=document.getElementById('chatmsg'),ab=document.getElementById('askBtn'),has=rows.length>0;
  if(ci&&ab){ci.disabled=!has;ab.disabled=!has;ci.placeholder=has?'Nhập câu hỏi khác cho Aurum…':'Thêm khoản vàng để trò chuyện cùng Aurum…';}
  var cb=document.getElementById('clearBtn');if(cb)cb.style.display=has?'':'none'; // ẩn "Xóa danh mục" khi chưa có khoản nào
  renderQuickPrompts(rows);
  var b=document.getElementById('pfBody');
  if(!rows.length){b.innerHTML='<tr><td colspan="7" class="muted l">Chưa có khoản nào. Thêm bên dưới hoặc dùng dữ liệu mẫu.</td></tr>';return;}
  b.innerHTML=rows.map(function(r){
    var s=(r.pnl>=0?'+':'−');
    var dt=r.buy_date?'<div class="lot-date">Lệnh mua '+fmtDate(r.buy_date)+'</div>':'';
    return '<tr><td class="l" data-label="Loại">'+r.label+dt+'</td><td data-label="SL (lượng)">'+fmt(r.qty)+'</td><td data-label="Giá mua">'+fmt(r.buy_price)+'M</td><td data-label="Giá thanh khoản">'+fmt(r.current)+'M</td>'+
    '<td data-label="Lời/Lỗ" class="'+cls(r.pnl)+'">'+s+fmt(Math.abs(r.pnl))+'M</td>'+
    '<td data-label="ROI" class="'+cls(r.pnl)+'">'+s+pct(r.roi,2)+'%</td>'+
    '<td class="act"><button class="del" onclick="delH(\\''+r.id+'\\')" title="Xóa">✕</button></td></tr>';
  }).join('');
}
function renderQuickPrompts(rows){
  var el=document.getElementById('qprompts'); if(!el)return;
  if(!rows||!rows.length){el.innerHTML='';return;}
  var pos=rows.filter(function(r){return r.roi>0;}).sort(function(a,b){return b.roi-a.roi;}); // tốt nhất trước
  var loss=rows.filter(function(r){return r.roi<0;}).sort(function(a,b){return a.roi-b.roi;}); // lỗ nặng nhất trước
  var best=pos[0], worst=loss[0];
  var bigWin=pos.find(function(r){return r.roi>5;});
  var chips=[];
  if(worst && best){
    // Hỗn hợp: ưu tiên xử lý mã lỗ nặng nhất, rồi cân bằng tâm lý bằng mã tốt nhất
    chips.push(worst.label+' đang chịu lỗ ngắn hạn, tôi nên giữ tiếp hay cắt lỗ?');
    chips.push('Có nên mua thêm '+best.label+' để tích sản tiếp không?');
  } else if(worst){
    // Chỉ có mã lỗ
    chips.push(worst.label+' đang chịu lỗ ngắn hạn, tôi nên giữ tiếp hay cắt lỗ?');
    chips.push('Giá '+worst.label+' xuống thấp, có nên mua thêm để trung bình giá?');
  } else if(bigWin){
    // Lời đậm (>5%)
    chips.push('Có nên mua thêm '+bigWin.label+' để tích sản tiếp không?');
    chips.push('Giá '+bigWin.label+' đang tốt, tôi có nên chốt lời một phần?');
  } else if(best){
    // Lời nhẹ (0-5%)
    chips.push('Có nên mua thêm '+best.label+' để tích sản tiếp không?');
  }
  chips.push('Hôm nay có thông tin gì bất lợi cho tôi?');
  el.innerHTML=chips.map(function(c){return '<button class="qp" onclick="askQuick(this.textContent)">'+c+'</button>';}).join('');
}
function askQuick(t){sendChat(t);}
// CTA động theo nhu cầu: mua thêm mã tốt nhất / bán cắt lỗ mã đang lỗ
// Action Card CHỈ hiện khi nhận định/tư vấn của Aurum thực sự gợi ý hành động (không hiện mặc định)
function showActionCard(reply){
  var el=document.getElementById('ctazone'); if(!el)return;
  var rows=window.lastRows||[];
  if(!rows.length||!reply){el.innerHTML='';return;}
  var r=String(reply).toLowerCase();
  var pos=rows.filter(function(x){return x.roi>0;}).sort(function(a,b){return b.roi-a.roi;});
  var loss=rows.filter(function(x){return x.roi<0;}).sort(function(a,b){return a.roi-b.roi;});
  var cr=document.getElementById('chatrow'),qp=document.getElementById('qprompts');
  var main,sub,target;
  if(/(tối ưu giá vốn|trung bình giá|cắt lỗ|tạm lỗ|lệnh lỗ)/.test(r)&&loss[0]){main='Tối ưu giá vốn';sub=loss[0].label+' · gợi ý bởi Aurum';target=loss[0].type;}
  else if(/(mua thêm|tích sản|gia tăng|nắm giữ|tích lũy)/.test(r)&&pos[0]){main='Tích sản thêm';sub=pos[0].label+' · gợi ý bởi Aurum';target=pos[0].type;}
  else{el.innerHTML='';if(cr)cr.style.display='';if(qp)qp.style.display='';return;}
  // Có giải pháp cụ thể: nút vàng tinh tế dưới lời thoại; tạm ẩn ô chat để user tập trung quyết định
  el.innerHTML='<button class="btn act-gold" onclick="openBuy(\\''+target+'\\')"><span class="ag-main">'+main+'</span><span class="ag-sub">'+sub+'</span></button>'+
    '<button class="ask-again" onclick="reopenChat()">Nhờ Aurum tư vấn thêm</button>';
  if(cr)cr.style.display='none';if(qp)qp.style.display='none';
}
function reopenChat(){var cr=document.getElementById('chatrow'),qp=document.getElementById('qprompts'),cz=document.getElementById('ctazone');if(cr)cr.style.display='';if(qp)qp.style.display='';if(cz)cz.innerHTML='';}
function openBuy(type){if(needName())return;if(type){document.getElementById('b_type').value=type;}
  document.getElementById('buyModal').classList.add('open');setMode('buy');}
function closeBuy(){document.getElementById('buyModal').classList.remove('open');}
function ownedQty(t){return (window.lastRows||[]).filter(function(r){return r.type===t;}).reduce(function(s,r){return s+(+r.qty||0);},0);}
function setMode(m){
  window.txnMode=m;
  document.getElementById('tabBuy').classList.toggle('active',m==='buy');
  document.getElementById('tabSell').classList.toggle('active',m==='sell');
  document.getElementById('txnBtn').textContent=(m==='sell')?'Xác nhận bán':'Thanh toán';
  var t=document.getElementById('b_type').value;
  if(m==='sell'){var av=ownedQty(t);document.getElementById('b_qty').value=av>0?Math.min(av,av):0.1;}
  else{document.getElementById('b_qty').value=0.1;}
  updModal();
}
function onTypeChange(){ if(window.txnMode==='sell'){var av=ownedQty(document.getElementById('b_type').value);document.getElementById('b_qty').value=av>0?av:0;} updModal(); }
function updModal(){
  var t=document.getElementById('b_type').value,q=parseFloat(document.getElementById('b_qty').value)||0;
  var p=(window.lastPrices||{})[t]||{};
  if(window.txnMode==='sell'){
    var price=p.buy||0,av=ownedQty(t);
    var warn=q>av?'<br><span style="color:#f87171">⚠ Vượt số đang có ('+fmt(av)+' lượng)</span>':'';
    document.getElementById('b_sum').innerHTML='Đang có: <b>'+fmt(av)+'</b> lượng · giá thanh khoản '+fmt(price)+'M/lượng<br>Nhận về: <b style="color:#4ade80;font-size:16px">'+fmt(price*q)+'M</b>'+warn;
  }else{
    var price=p.sell||0;
    document.getElementById('b_sum').innerHTML='Giá bán ra hiện tại: <b>'+fmt(price)+'M</b>/lượng<br>Tổng thanh toán: <b style="color:#f5c542;font-size:16px">'+fmt(price*q)+'M</b>';
  }
}
async function confirmTxn(){
  var t=document.getElementById('b_type').value,q=parseFloat(document.getElementById('b_qty').value)||0;
  if(q<=0){alert('Nhập số lượng hợp lệ');return;}
  var p=(window.lastPrices||{})[t]||{};
  if(window.txnMode==='sell'){
    var av=ownedQty(t);
    if(q>av){alert('Bạn chỉ đang có '+fmt(av)+' lượng '+(((window.lastPrices||{})[t]||{}).label||t)+'. Không thể bán quá số lượng đang có.');return;}
    var price=p.buy||0;closeBuy();
    setIns('<span class="muted">✅ Bán '+fmt(q)+' lượng thành công (giả lập). Nhận <b>'+fmt(price*q)+'M</b> về ví Zalopay. Đang cập nhật danh mục…</span>');
    var r=await api({action:'pf_sell',type:t,qty:q});render(r);autoInsight();
  }else{
    var price=p.sell||0;
    var dt=new Date(),ds=dt.getFullYear()+'-'+('0'+(dt.getMonth()+1)).slice(-2)+'-'+('0'+dt.getDate()).slice(-2);
    closeBuy();
    setIns('<span class="muted">✅ Thanh toán <b>'+fmt(price*q)+'M</b> qua Zalopay thành công (giả lập). Đang thêm '+fmt(q)+' lượng vào danh mục…</span>');
    var r=await api({action:'pf_add',holding:{type:t,qty:q,buy_price:price,buy_date:ds}});render(r);autoInsight();
  }
}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function renderNews(items,replyText){
  var el=document.getElementById('newsbox'); if(!el)return;
  // Chỉ hiện link bài báo khi nhận định AI thực sự nhắc tới tin tức/thị trường
  var relevant=replyText&&/tin tức|thị trường|phiên|thế giới|điều chỉnh|đỉnh|Fed|Mỹ|Iran/i.test(replyText);
  if(!items||!items.length||!relevant){el.innerHTML='';return;}
  el.innerHTML='<details class="news-acc"><summary>📰 Tin Aurum đang tham khảo ('+items.length+')</summary>'+items.map(function(a){
    return '<a class="nb-i" href="'+esc(a.link)+'" target="_blank" rel="noopener">'+esc(a.source)+': '+esc(a.title)+' ↗</a>';
  }).join('')+'</details>';
}
function needName(){if(!getUser()){openName();return true;}return false;}
async function load(){
  if(!getUser()){
    setIns('<span class="muted">👋 Nhập <b>tên của bạn</b> ở ô trên cùng để Aurum bắt đầu theo dõi tài sản vàng giúp bạn.</span>');
    document.getElementById('mkt').innerHTML='<span class="mk-sub">Nhập tên để Aurum tải giá thị trường &amp; theo dõi danh mục cho bạn.</span>';
    render({rows:[],totals:{},holdings_raw:[]});
    openName();
    return;
  }
  var d=await api({action:'pf_list'});render(d);autoInsight();
}
function openAdd(){if(needName())return;document.getElementById('f_qty').value='';document.getElementById('f_price').value='';document.getElementById('f_date').value='';document.getElementById('addModal').classList.add('open');}
function closeAdd(){document.getElementById('addModal').classList.remove('open');}
function toISO(s){if(!s)return '';var p=String(s).trim().split('/');return p.length===3?(p[2]+'-'+('0'+p[1]).slice(-2)+'-'+('0'+p[0]).slice(-2)):'';}
async function confirmAdd(){
  var qty=document.getElementById('f_qty').value;
  var np=normPrice(document.getElementById('f_price').value);
  if(!qty||!np.v){alert('Vui lòng nhập số lượng và giá mua');return;}
  if(np.v<10||np.v>1000){alert('Giá mua "'+fmt(np.v)+'" có vẻ không hợp lệ. Hãy nhập giá theo triệu đồng/lượng — VD: 150 nghĩa là 150 triệu/lượng.');return;}
  var h={type:document.getElementById('f_type').value,qty:qty,
    buy_price:np.v,buy_date:toISO(document.getElementById('f_date').value)};
  closeAdd();
  if(np.note)showToast(np.note);
  var d=await api({action:'pf_add',holding:h});
  if(d&&d.status==='error'){alert(d.message||'Không thêm được tài sản.');return;}
  render(d);autoInsight();
}
async function delH(id){var d=await api({action:'pf_delete',id:id});render(d);autoInsight();}
async function seedDemo(){if(needName())return;var d=await api({action:'pf_seed_demo'});render(d);autoInsight();}
async function clearPortfolio(){if(needName())return;if(!(window.lastRows||[]).length){return;}if(!confirm('Xóa toàn bộ danh mục của bạn?'))return;var d=await api({action:'pf_clear'});render(d);autoInsight();}
async function autoInsight(){
  if(!(window.lastHoldings||[]).length){setIns('<span class="muted">👋 Chào bạn! Aurum sẽ theo dõi lời/lỗ tài sản vàng giúp bạn. Hãy bấm <b>"+ Thêm tài sản"</b> hoặc <b>"Dùng dữ liệu mẫu"</b> bên dưới để bắt đầu.</span>');renderNews([]);showActionCard('');return;}
  setIns('<span class="spin"></span> <span class="muted">Đang phân tích tài sản của bạn…</span>');
  var d=await api({action:'ai_insight',holdings:window.lastHoldings||[]});setIns(d.reply||'—');renderNews(d.news_items,d.reply);showActionCard(d.reply);
}
async function sendChat(forceMsg){
  var m=((typeof forceMsg==='string'?forceMsg:'')||document.getElementById('chatmsg').value).trim();if(!m)return;
  setIns('<span class="spin"></span> <span class="muted">Aurum đang trả lời…</span>');
  document.getElementById('chatmsg').value='';
  var d=await api({action:'chat',message:m,holdings:window.lastHoldings||[]});setIns(d.reply||'—');renderNews(d.news_items,d.reply);showActionCard(d.reply);
}
async function refreshData(){if(!getUser())return;var d=await api({action:'pf_list'});render(d);}
window.refreshLeft=60;
setInterval(function(){
  if(!getUser())return;
  window.refreshLeft--;
  var el=document.getElementById('mktCountdown');if(el)el.textContent=window.refreshLeft;
  if(window.refreshLeft<=0){window.refreshLeft=60;refreshData();}
},1000);
(function(){load();})();
</script>
</body></html>"""


async def _companion_page(request):
    from starlette.responses import HTMLResponse
    return HTMLResponse(COMPANION_HTML)


async def _market_page(request):
    from starlette.responses import HTMLResponse
    try:
        _, _, _, _, page = _build_all()
        return HTMLResponse(page)
    except Exception as e:
        return HTMLResponse(f"<h1>Lỗi tải dữ liệu</h1><pre>{html.escape(str(e))}</pre>", status_code=500)


async def _data_json(request):
    from starlette.responses import JSONResponse
    return JSONResponse(build_market_data())


app.add_route("/", _companion_page, methods=["GET"])
app.add_route("/market", _market_page, methods=["GET"])
app.add_route("/data.json", _data_json, methods=["GET"])


if __name__ == "__main__":
    app.run(port=8080, host="0.0.0.0")
