#!/usr/bin/env python3
"""
Kalshi <-> Polymarket arbitrage scanner.

Pulls open markets from both public APIs (no login needed), fuzzy-matches
markets that look like the same question, and flags pairs where buying
YES on one site + NO on the other costs less than $1 after fees.

For pairs that clear the minimum edge it then:
  * walks both order books to see how many contracts you could actually buy
    before the edge disappears (top-of-book prices are often only a few
    contracts deep), and
  * compares the two markets' resolution rules and flags differences in
    thresholds, "above" vs "at least", resolution sources, dates, and
    Polymarket's 50-50 clause.

Usage:
    python3 arb_scanner.py                  # default settings
    python3 arb_scanner.py --min-edge 0.01 --min-sim 0.6
    python3 arb_scanner.py --poly-fee 0.0   # if your Polymarket account has no fees
    python3 arb_scanner.py --no-depth       # skip order book lookups (faster)

Outputs:
    arb_opportunities.csv   - every candidate pair, best first
    arb_report.html         - same thing, readable in a browser

IMPORTANT: matches are fuzzy and the rules check is a heuristic. ALWAYS read
both markets' resolution rules before trading. Two questions that look
identical can resolve differently. This is a research tool, not financial advice.

Only uses the Python standard library (Python 3.8+).
"""

import argparse
import csv
import html
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher

KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2/markets"
POLY_URL = "https://gamma-api.polymarket.com/markets"
KALSHI_BOOK_URL = "https://api.elections.kalshi.com/trade-api/v2/markets/{}/orderbook"
POLY_BOOK_URL = "https://clob.polymarket.com/book"
UA = {"User-Agent": "arb-scanner/1.0", "Accept": "application/json"}


# ---------------------------------------------------------------- fetching
def get_json(url, params=None, retries=3):
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(full, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt + 1} after error: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_kalshi(max_pages=30):
    """Open Kalshi markets with YES/NO ask prices in dollars."""
    out, cursor = [], None
    for _ in range(max_pages):
        params = {"status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = get_json(KALSHI_URL, params)
        for m in data.get("markets", []):
            # API has used both cents (yes_ask) and dollar strings (yes_ask_dollars)
            ya = num(m.get("yes_ask_dollars"))
            na = num(m.get("no_ask_dollars"))
            if ya is None and num(m.get("yes_ask")) is not None:
                ya = num(m.get("yes_ask")) / 100
            if na is None and num(m.get("no_ask")) is not None:
                na = num(m.get("no_ask")) / 100
            if not ya or not na or ya >= 1 or na >= 1:
                continue  # no live offer on one side
            title = m.get("title") or ""
            sub = m.get("yes_sub_title") or m.get("subtitle") or ""
            if sub and sub.lower() not in title.lower():
                title = f"{title} — {sub}"
            out.append({
                "site": "Kalshi",
                "id": m.get("ticker"),
                "title": title,
                "yes_ask": ya,
                "no_ask": na,
                "close": m.get("close_time") or m.get("expiration_time"),
                "url": f"https://kalshi.com/markets/{(m.get('event_ticker') or m.get('ticker') or '').lower()}",
                "volume": num(m.get("volume")) or 0,
                "rules": "\n\n".join(filter(None, [m.get("rules_primary"), m.get("rules_secondary")])),
                "strikes": [s for s in (num(m.get("floor_strike")), num(m.get("cap_strike"))) if s is not None],
            })
        cursor = data.get("cursor")
        print(f"  Kalshi: {len(out)} markets so far", file=sys.stderr)
        if not cursor:
            break
    return out


def fetch_polymarket(max_pages=40):
    """Open binary Polymarket markets with YES/NO ask prices."""
    out, offset, page = [], 0, 500
    for _ in range(max_pages):
        data = get_json(POLY_URL, {"active": "true", "closed": "false",
                                   "limit": page, "offset": offset})
        if not data:
            break
        for m in data:
            try:
                outcomes = json.loads(m.get("outcomes") or "[]")
            except json.JSONDecodeError:
                continue
            if [o.lower() for o in outcomes] != ["yes", "no"]:
                continue
            best_ask = num(m.get("bestAsk"))   # price to BUY yes
            best_bid = num(m.get("bestBid"))   # buying NO ~= 1 - best yes bid
            if not best_ask or not best_bid or best_ask >= 1 or best_bid <= 0:
                continue
            try:
                tokens = json.loads(m.get("clobTokenIds") or "[]")
            except json.JSONDecodeError:
                tokens = []
            out.append({
                "site": "Polymarket",
                "id": m.get("slug") or m.get("id"),
                "title": m.get("question") or "",
                "yes_ask": best_ask,
                "no_ask": round(1 - best_bid, 4),
                "close": m.get("endDate"),
                "url": f"https://polymarket.com/market/{m.get('slug', '')}",
                "volume": num(m.get("volume")) or 0,
                "rules": m.get("description") or "",
                "source": m.get("resolutionSource") or "",
                "yes_token": tokens[0] if tokens else None,  # order matches outcomes: [Yes, No]
            })
        offset += page
        print(f"  Polymarket: {len(out)} markets so far", file=sys.stderr)
        if len(data) < page:
            break
    return out


# ---------------------------------------------------------------- matching
STOP = set("""will the a an of in on by to be for at is and or before after
than more less with from end does do this that who what which""".split())


def norm(t):
    t = t.lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9.%$ ]+", " ", t)
    return [w for w in t.split() if w not in STOP]


def numbers(t):
    return set(re.findall(r"\d+(?:\.\d+)?", t.replace(",", "")))


def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def similarity(a, b):
    ta, tb = norm(a["title"]), norm(b["title"])
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    jacc = len(sa & sb) / len(sa | sb)
    seq = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    score = 0.6 * jacc + 0.4 * seq
    # Different numbers ("above 50%" vs "above 55%") are usually different markets
    na, nb = numbers(a["title"]), numbers(b["title"])
    if na and nb and na != nb:
        score *= 0.5
    return score


def match(kalshi, poly, min_sim, max_days):
    # Index Polymarket by keyword so we don't compare every pair
    index = {}
    for i, p in enumerate(poly):
        for w in set(norm(p["title"])):
            if len(w) > 3:
                index.setdefault(w, set()).add(i)
    pairs = []
    for k in kalshi:
        cand = set()
        for w in set(norm(k["title"])):
            if len(w) > 3:
                cand |= index.get(w, set())
        best = None
        for i in cand:
            p = poly[i]
            kd, pd = parse_dt(k["close"]), parse_dt(p["close"])
            if kd and pd and abs((kd - pd).days) > max_days:
                continue
            s = similarity(k, p)
            if s >= min_sim and (best is None or s > best[0]):
                best = (s, p)
        if best:
            pairs.append((best[0], k, best[1]))
    return pairs


# ---------------------------------------------------------------- rules check
INCLUSIVE = re.compile(r"\b(at least|or more|or higher|or above|or greater|greater than or equal|"
                       r"no less than|equal to or (?:greater|more|higher))\b|>=|≥")
STRICT = re.compile(r"\b(more than|greater than|above|exceeds?|higher than|over)\b")
SOURCES = {
    "AP": r"associated press|\bap\b", "NBC": r"\bnbc\b", "CNN": r"\bcnn\b", "Fox": r"\bfox\b",
    "Decision Desk": r"decision desk", "BLS": r"bureau of labor statistics|\bbls\b",
    "BEA": r"bureau of economic analysis|\bbea\b", "Fed": r"federal reserve|\bfomc\b",
    "Coinbase": r"coinbase", "Binance": r"binance", "Kraken": r"kraken", "CoinGecko": r"coingecko",
    "CoinMarketCap": r"coinmarketcap", "CME": r"\bcme\b", "Chainlink": r"chainlink", "Pyth": r"\bpyth\b",
    "Bloomberg": r"bloomberg", "Reuters": r"reuters", "Yahoo Finance": r"yahoo finance",
    "ESPN": r"\bespn\b", "Wikipedia": r"wikipedia", "Box Office Mojo": r"box office mojo",
}


def key_numbers(text):
    """Numbers that look like thresholds, ignoring years and day-of-month."""
    out = set()
    for n in re.findall(r"\d+(?:\.\d+)?", text.replace(",", "")):
        v = float(n)
        if "." not in n and (v <= 31 or 1900 <= v <= 2100):
            continue
        out.add(v)
    return out


def rule_thresholds(text):
    """Dollar amounts and percentages written in rules text ("$145,000", "4.5%", "$2.5k")."""
    out = set()
    for m in re.finditer(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?([kmb])?\b|(\d[\d,]*(?:\.\d+)?)\s?%", text.lower()):
        v = float((m.group(1) or m.group(3)).replace(",", ""))
        v *= {"k": 1e3, "m": 1e6, "b": 1e9}.get(m.group(2) or "", 1)
        if m.group(1) and v <= 1:
            continue  # "$1 per contract" payout boilerplate
        out.add(v)
    return out


def inclusivity(text):
    t = text.lower()
    inc = bool(INCLUSIVE.search(t))
    strict = bool(STRICT.search(INCLUSIVE.sub(" ", t)))
    if inc and not strict:
        return "inclusive"
    if strict and not inc:
        return "strict"
    return None


def sources(text):
    t = text.lower()
    return {name for name, pat in SOURCES.items() if re.search(pat, t)}


def rules_check(k, p):
    """Heuristic comparison of resolution rules. Returns (status, notes)."""
    kr, pr = k.get("rules") or "", " ".join(filter(None, [p.get("rules"), p.get("source")]))
    notes, status = [], "ok"

    def flag(level, msg):
        nonlocal status
        notes.append(msg)
        if level == "mismatch" or status == "ok":
            status = level

    if not kr or not pr:
        flag("review", "rules text missing on " + ("Kalshi" if not kr else "Polymarket") + " — read manually")
    # Every threshold in one market should show up somewhere in the other
    k_nums = key_numbers(k["title"]) | {s for s in k.get("strikes", []) if s > 31}
    p_nums = key_numbers(p["title"])
    k_all, p_all = k_nums | key_numbers(kr), p_nums | key_numbers(pr)
    if kr and pr:
        miss_k = sorted(n for n in k_nums | rule_thresholds(kr) if n not in p_all)
        miss_p = sorted(n for n in p_nums | rule_thresholds(pr) if n not in k_all)
        if miss_k:
            flag("mismatch", "Kalshi threshold not in Polymarket rules: " + ", ".join(f"{n:g}" for n in miss_k))
        if miss_p:
            flag("mismatch", "Polymarket threshold not in Kalshi rules: " + ", ".join(f"{n:g}" for n in miss_p))
    ki = inclusivity(kr) or inclusivity(k["title"])
    pi = inclusivity(pr) or inclusivity(p["title"])
    if ki and pi and ki != pi:
        flag("review", f"boundary differs: Kalshi looks {ki}, Polymarket looks {pi} (\"above\" vs \"at least\")")
    ks, ps = sources(kr), sources(pr)
    if ks and ps and not ks & ps:
        flag("review", f"different resolution sources: Kalshi {', '.join(sorted(ks))} vs Polymarket {', '.join(sorted(ps))}")
    kd, pd = parse_dt(k["close"]), parse_dt(p["close"])
    if kd and pd and abs((kd - pd).total_seconds()) > 86400:
        flag("review", f"close dates differ by {abs((kd - pd).days)}d")
    if re.search(r"50\s*-\s*50|fifty.fifty", pr.lower()):
        flag("review", "Polymarket can resolve 50-50 (pays $0.50), which breaks the hedge")
    return status, notes


# ---------------------------------------------------------------- order books
def _levels(raw, cents=False):
    """[[price, size], ...] or [{price, size}, ...] -> [(price, size)] in dollars."""
    out = []
    for lv in raw or []:
        p, s = (lv.get("price"), lv.get("size")) if isinstance(lv, dict) else (lv[0], lv[1])
        p, s = num(p), num(s)
        if p is None or s is None:
            continue
        if cents:
            p /= 100
        if 0 < p < 1 and s > 0:
            out.append((p, s))
    return out


def kalshi_asks(ticker):
    """Cheapest-first ask ladders {"yes_ask": [...], "no_ask": [...]}.

    Kalshi's book only lists bids: a NO bid at p is an offer to sell YES at 1-p,
    so buying YES walks the NO bids and vice versa.
    """
    data = get_json(KALSHI_BOOK_URL.format(urllib.parse.quote(ticker, safe="")))
    ob = data.get("orderbook_fp") or data.get("orderbook") or {}
    if "yes_dollars" in ob or "no_dollars" in ob:
        yes, no = _levels(ob.get("yes_dollars")), _levels(ob.get("no_dollars"))
    else:
        yes, no = _levels(ob.get("yes"), cents=True), _levels(ob.get("no"), cents=True)
    return {"yes_ask": sorted((round(1 - p, 4), s) for p, s in no),
            "no_ask": sorted((round(1 - p, 4), s) for p, s in yes)}


def poly_asks(yes_token):
    """Cheapest-first ask ladders from the YES token's CLOB book.

    Buying NO at 1-p is the mirror of selling YES into a bid at p.
    """
    data = get_json(POLY_BOOK_URL, {"token_id": yes_token})
    return {"yes_ask": sorted(_levels(data.get("asks"))),
            "no_ask": sorted((round(1 - p, 4), s) for p, s in _levels(data.get("bids")))}


def walk_books(k_ladder, p_ladder, poly_fee, min_edge, max_contracts):
    """Buy one contract on each leg at a time, cheapest first, while the next
    contract still clears min_edge after fees.

    Returns (contracts, total_cost_incl_fees). The Kalshi fee is rounded up
    to the cent once on the whole fill, as for a single order.
    """
    k = [list(l) for l in k_ladder]
    p = [list(l) for l in p_ladder]
    ki = pi = 0
    n = base = kfee_raw = 0.0
    while ki < len(k) and pi < len(p) and n < max_contracts:
        kp, pp = k[ki][0], p[pi][0]
        unit = kp + 0.07 * kp * (1 - kp) + pp * (1 + poly_fee)
        if 1 - unit < min_edge:
            break
        q = min(k[ki][1], p[pi][1], max_contracts - n)
        n += q
        base += q * (kp + pp * (1 + poly_fee))
        kfee_raw += 0.07 * q * kp * (1 - kp)
        k[ki][1] -= q
        p[pi][1] -= q
        if k[ki][1] <= 1e-9:
            ki += 1
        if p[pi][1] <= 1e-9:
            pi += 1
    cost = base + math.ceil(round(kfee_raw * 100, 6)) / 100
    return n, cost


def add_depth(row, k, p, poly_fee, min_edge, max_contracts):
    k_side, p_side = row["_sides"]
    row["depth_note"] = ""
    if not p.get("yes_token"):
        row["depth_note"] = "no Polymarket token id"
        return
    try:
        kb, pb = kalshi_asks(k["id"]), poly_asks(p["yes_token"])
    except Exception as e:  # noqa: BLE001
        row["depth_note"] = f"book fetch failed: {e}"
        return
    n, cost = walk_books(kb[k_side], pb[p_side], poly_fee, min_edge, max_contracts)
    row["depth_contracts"] = round(n, 2)
    row["depth_cost_$"] = round(cost, 2)
    row["depth_profit_$"] = round(n - cost, 2)
    if n == 0:
        row["depth_note"] = "edge gone at live book prices"
    elif n >= max_contracts:
        row["depth_note"] = f"capped at --max-contracts {max_contracts}"


# ---------------------------------------------------------------- pricing
def kalshi_fee(price, contracts=100):
    """Kalshi taker fee: ceil(0.07 * C * P * (1-P)) to the cent, per contract."""
    total = math.ceil(0.07 * contracts * price * (1 - price) * 100) / 100
    return total / contracts


def evaluate(sim, k, p, poly_fee):
    now = datetime.now(timezone.utc)
    rows = []
    # Option A: YES on Kalshi + NO on Polymarket
    # Option B: NO on Kalshi + YES on Polymarket
    for label, k_side, p_side in (("YES Kalshi + NO Poly", "yes_ask", "no_ask"),
                                  ("NO Kalshi + YES Poly", "no_ask", "yes_ask")):
        kp, pp = k[k_side], p[p_side]
        cost = kp + kalshi_fee(kp) + pp * (1 + poly_fee)
        edge = 1 - cost
        close = max(filter(None, [parse_dt(k["close"]), parse_dt(p["close"])]), default=None)
        days = max((close - now).days, 1) if close else None
        annual = (edge / cost) * (365 / days) if days and cost > 0 else None
        rows.append({
            "similarity": round(sim, 3),
            "strategy": label,
            "kalshi_price": round(kp, 4),
            "poly_price": round(pp, 4),
            "total_cost_after_fees": round(cost, 4),
            "profit_per_$1_payout": round(edge, 4),
            "return_pct": round(100 * edge / cost, 2),
            "days_to_close": days,
            "annualized_pct": round(100 * annual, 1) if annual is not None else None,
            "kalshi_title": k["title"],
            "poly_title": p["title"],
            "kalshi_close": k["close"],
            "poly_close": p["close"],
            "kalshi_url": k["url"],
            "poly_url": p["url"],
            "depth_contracts": None,
            "depth_cost_$": None,
            "depth_profit_$": None,
            "depth_note": "not checked",
            "_sides": (k_side, p_side),
        })
    best = max(rows, key=lambda r: r["profit_per_$1_payout"])
    status, notes = rules_check(k, p)
    best["rules_status"] = status
    best["rules_notes"] = "; ".join(notes)
    best["_kalshi_rules"] = k.get("rules") or ""
    best["_k"], best["_p"] = k, p
    best["_poly_rules"] = "\n\n".join(filter(None, [p.get("rules"), p.get("source") and "Resolution source: " + p["source"]]))
    return best


# ---------------------------------------------------------------- output
def write_csv(rows, path):
    if not rows:
        open(path, "w").close()
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[c for c in rows[0] if not c.startswith("_")], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def is_good(r, min_edge):
    """Clears the edge, the rules check didn't find a threshold mismatch, and
    (if the books were checked) at least one contract is fillable."""
    return (r["profit_per_$1_payout"] >= min_edge
            and r["rules_status"] != "mismatch"
            and (r["depth_note"] == "not checked" or (r["depth_contracts"] or 0) > 0))


def depth_text(r):
    if r["depth_note"] == "not checked":
        return "book not checked"
    if r["depth_contracts"] is None:
        return r["depth_note"]
    txt = f"{r['depth_contracts']:g} contracts · ${r['depth_cost_$']:g} in → ${r['depth_profit_$']:g} profit"
    return txt + (f" ({r['depth_note']})" if r["depth_note"] else "")


def write_html(rows, path, min_edge):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    e = html.escape
    body = []
    for r in rows:
        notes = "".join(f"<li>{e(n)}</li>" for n in r["rules_notes"].split("; ") if n)
        cls = ("good" if r["rules_status"] == "ok" else "maybe") if is_good(r, min_edge) else ""
        body.append(f"""<tr class="{cls}">
<td><b>{r['return_pct']}%</b><br><small>{r['annualized_pct']}%/yr · {r['days_to_close']}d</small></td>
<td>{e(r['strategy'])}<br><small>K {r['kalshi_price']} + P {r['poly_price']} = {r['total_cost_after_fees']}</small>
<br><small>{e(depth_text(r))}</small></td>
<td><a href="{e(r['kalshi_url'])}">{e(r['kalshi_title'])}</a><br>
<a href="{e(r['poly_url'])}">{e(r['poly_title'])}</a><br><small>match {r['similarity']}</small>
<div class="rules {e(r['rules_status'])}">rules: {e(r['rules_status'])}<ul>{notes}</ul></div>
<details><summary>Compare resolution rules</summary><div class="cmp">
<div><b>Kalshi</b><p>{e(r['_kalshi_rules']) or '<i>none returned</i>'}</p></div>
<div><b>Polymarket</b><p>{e(r['_poly_rules']) or '<i>none returned</i>'}</p></div></div></details></td></tr>""")
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Arb Scan</title>
<style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}}
table{{border-collapse:collapse;width:100%}}td{{border-bottom:1px solid #ddd;padding:8px;vertical-align:top}}
tr.good{{background:#e8f6ec}}tr.maybe{{background:#fdf6e3}}small{{color:#666}}a{{color:#1a5fb4}}
.warn{{background:#fff4d6;padding:10px;border-radius:6px}}
.rules{{font-size:13px;margin-top:4px}}.rules ul{{margin:2px 0 0 18px;padding:0}}
.rules.ok{{color:#26734d}}.rules.review{{color:#8a5a00}}.rules.mismatch{{color:#b42318;font-weight:600}}
.cmp{{display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:13px}}.cmp p{{white-space:pre-wrap;margin:4px 0}}
@media (max-width:700px){{.cmp{{grid-template-columns:1fr}}}}
</style></head><body>
<h2>Kalshi ↔ Polymarket scan — {stamp}</h2>
<p class="warn"><b>Check before trading:</b> matches and the rules check are automatic heuristics.
Open "Compare resolution rules" and read both sides, and remember money is locked until resolution.
Green rows clear your {min_edge*100:.1f}% minimum edge after fees, pass the rules check, and (where the
order books were checked) have at least one contract fillable at that edge. Amber rows do too, but the
rules check found something to read closely.</p>
<table>{''.join(body) or '<tr><td>No matched markets found.</td></tr>'}</table></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-sim", type=float, default=0.55, help="title match threshold 0-1 (default 0.55)")
    ap.add_argument("--min-edge", type=float, default=0.01, help="highlight pairs with at least this profit per $1 (default 0.01)")
    ap.add_argument("--max-days-apart", type=int, default=7, help="max gap between close dates (default 7)")
    ap.add_argument("--poly-fee", type=float, default=0.01, help="Polymarket fee as fraction of price (default 0.01; set 0 if you pay none)")
    ap.add_argument("--show", type=int, default=25, help="rows to print (default 25)")
    ap.add_argument("--no-depth", action="store_true", help="skip order book lookups")
    ap.add_argument("--depth-top", type=int, default=40, help="check order books for at most this many pairs (default 40)")
    ap.add_argument("--max-contracts", type=float, default=1000, help="stop walking the books at this many contracts (default 1000)")
    a = ap.parse_args()

    print("Fetching Kalshi...", file=sys.stderr)
    kalshi = fetch_kalshi()
    print("Fetching Polymarket...", file=sys.stderr)
    poly = fetch_polymarket()
    print(f"Matching {len(kalshi)} Kalshi x {len(poly)} Polymarket markets...", file=sys.stderr)

    pairs = match(kalshi, poly, a.min_sim, a.max_days_apart)
    rows = sorted((evaluate(s, k, p, a.poly_fee) for s, k, p in pairs),
                  key=lambda r: r["profit_per_$1_payout"], reverse=True)

    # Deeper levels only cost more, so only pairs that clear the edge at the
    # top of the book can be worth walking.
    if not a.no_depth:
        todo = [r for r in rows if r["profit_per_$1_payout"] >= a.min_edge
                and r["rules_status"] != "mismatch"][: a.depth_top]
        for i, r in enumerate(todo, 1):
            print(f"  order books {i}/{len(todo)}", file=sys.stderr)
            add_depth(r, r["_k"], r["_p"], a.poly_fee, a.min_edge, a.max_contracts)

    write_csv(rows, "arb_opportunities.csv")
    write_html(rows, "arb_report.html", a.min_edge)

    hits = [r for r in rows if is_good(r, a.min_edge)]
    print(f"\n{len(pairs)} matched pairs, {len(hits)} with edge >= {a.min_edge*100:.1f}% after fees "
          f"that pass the rules and order book checks\n")
    for r in rows[: a.show]:
        flag = ("✅" if r["rules_status"] == "ok" else "⚠️") if is_good(r, a.min_edge) else "  "
        print(f"{flag} {r['return_pct']:>6}%  {r['strategy']:<22} match={r['similarity']}  rules={r['rules_status']}")
        print(f"     K: {r['kalshi_title'][:90]}")
        print(f"     P: {r['poly_title'][:90]}")
        print(f"     book: {depth_text(r)}")
        if r["rules_notes"]:
            print(f"     rules: {r['rules_notes'][:200]}")
        print()
    print("Full results: arb_report.html (open in browser) and arb_opportunities.csv")


if __name__ == "__main__":
    main()
