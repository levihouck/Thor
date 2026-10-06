#!/usr/bin/env python3
"""
Kalshi <-> Polymarket arbitrage scanner.

Pulls open markets from both public APIs (no login needed), fuzzy-matches
markets that look like the same question, and flags pairs where buying
YES on one site + NO on the other costs less than $1 after fees.

Usage:
    python3 arb_scanner.py                  # default settings
    python3 arb_scanner.py --min-edge 0.01 --min-sim 0.6
    python3 arb_scanner.py --poly-fee 0.0   # if your Polymarket account has no fees

Outputs:
    arb_opportunities.csv   - every candidate pair, best first
    arb_report.html         - same thing, readable in a browser

IMPORTANT: matches are fuzzy. ALWAYS read both markets' resolution rules
before trading. Two questions that look identical can resolve differently.
This is a research tool, not financial advice.

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
UA = {"User-Agent": "arb-scanner/1.0", "Accept": "application/json"}


# ---------------------------------------------------------------- fetching
def get_json(url, params, retries=3):
    full = url + "?" + urllib.parse.urlencode(params)
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
            out.append({
                "site": "Polymarket",
                "id": m.get("slug") or m.get("id"),
                "title": m.get("question") or "",
                "yes_ask": best_ask,
                "no_ask": round(1 - best_bid, 4),
                "close": m.get("endDate"),
                "url": f"https://polymarket.com/market/{m.get('slug', '')}",
                "volume": num(m.get("volume")) or 0,
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
        })
    return max(rows, key=lambda r: r["profit_per_$1_payout"])


# ---------------------------------------------------------------- output
def write_csv(rows, path):
    if not rows:
        open(path, "w").close()
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_html(rows, path, min_edge):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    body = []
    for r in rows:
        good = r["profit_per_$1_payout"] >= min_edge
        e = html.escape
        body.append(f"""<tr class="{'good' if good else ''}">
<td><b>{r['return_pct']}%</b><br><small>{r['annualized_pct']}%/yr · {r['days_to_close']}d</small></td>
<td>{e(r['strategy'])}<br><small>K {r['kalshi_price']} + P {r['poly_price']} = {r['total_cost_after_fees']}</small></td>
<td><a href="{e(r['kalshi_url'])}">{e(r['kalshi_title'])}</a><br>
<a href="{e(r['poly_url'])}">{e(r['poly_title'])}</a><br><small>match {r['similarity']}</small></td></tr>""")
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Arb Scan</title>
<style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#fafafa;color:#222}}
table{{border-collapse:collapse;width:100%}}td{{border-bottom:1px solid #ddd;padding:8px;vertical-align:top}}
tr.good{{background:#e8f6ec}}small{{color:#666}}a{{color:#1a5fb4}}
.warn{{background:#fff4d6;padding:10px;border-radius:6px}}
</style></head><body>
<h2>Kalshi ↔ Polymarket scan — {stamp}</h2>
<p class="warn"><b>Check before trading:</b> matches are automatic. Read both resolution rules,
confirm the order book has enough size at these prices, and remember money is locked until resolution.
Green rows clear your {min_edge*100:.1f}% minimum edge after fees.</p>
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
    a = ap.parse_args()

    print("Fetching Kalshi...", file=sys.stderr)
    kalshi = fetch_kalshi()
    print("Fetching Polymarket...", file=sys.stderr)
    poly = fetch_polymarket()
    print(f"Matching {len(kalshi)} Kalshi x {len(poly)} Polymarket markets...", file=sys.stderr)

    pairs = match(kalshi, poly, a.min_sim, a.max_days_apart)
    rows = sorted((evaluate(s, k, p, a.poly_fee) for s, k, p in pairs),
                  key=lambda r: r["profit_per_$1_payout"], reverse=True)

    write_csv(rows, "arb_opportunities.csv")
    write_html(rows, "arb_report.html", a.min_edge)

    hits = [r for r in rows if r["profit_per_$1_payout"] >= a.min_edge]
    print(f"\n{len(pairs)} matched pairs, {len(hits)} with edge >= {a.min_edge*100:.1f}% after fees\n")
    for r in rows[: a.show]:
        flag = "✅" if r["profit_per_$1_payout"] >= a.min_edge else "  "
        print(f"{flag} {r['return_pct']:>6}%  {r['strategy']:<22} match={r['similarity']}")
        print(f"     K: {r['kalshi_title'][:90]}")
        print(f"     P: {r['poly_title'][:90]}\n")
    print("Full results: arb_report.html (open in browser) and arb_opportunities.csv")


if __name__ == "__main__":
    main()
