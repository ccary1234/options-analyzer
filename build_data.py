#!/usr/bin/env python3
"""
Options Premium Screener - nightly data builder.

Reads basket.txt, pulls delayed option chains (CBOE public delayed-quotes JSON,
falling back to Yahoo via yfinance), plus price history, earnings dates,
dividends and headlines (yfinance), trims the chains to what the screener needs,
and writes data.json for the static front-end.

Usage:
    python build_data.py                # real data
    python build_data.py --demo         # synthetic data, no network needed
    python build_data.py --out docs/data.json --basket basket.txt

No API keys required.
"""

import argparse
import datetime as dt
import json
import math
import random
import re
import sys
import time
import urllib.request

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

MAX_DTE = 120            # keep expirations out to this many days
MONEYNESS_BAND = 0.30    # keep strikes within +/-30% of spot
MIN_OPEN_INTEREST = 0    # set to e.g. 10 to drop dead strikes
NEWS_ITEMS = 6
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
UA = {"User-Agent": "Mozilla/5.0 (options-screener; personal use)"}

OCC_RE = re.compile(r"^([A-Z.]+?)(\d{6})([CP])(\d{8})$")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(spot, strike, dte_days, iv, is_call, r=0.04):
    """Black-Scholes delta, used only when the data source doesn't supply one."""
    if spot <= 0 or strike <= 0 or dte_days <= 0 or not iv or iv <= 0:
        return None
    t = dte_days / 365.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
    return norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0


def parse_occ(sym):
    """AAPL260918C00230000 -> (root, date, 'C'|'P', strike)"""
    m = OCC_RE.match(sym.replace(" ", ""))
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    date = dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    return root, date, cp, int(strike) / 1000.0


def clean(v, nd=4):
    """JSON-safe float or None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, nd)


def read_basket(path):
    out = []
    with open(path) as fh:
        for line in fh:
            s = line.split("#", 1)[0].strip().upper()
            if s:
                out.append(s)
    return out


def trim_chain(rows, spot, today):
    """
    rows: list of dicts {exp: date, cp: 'C'|'P', strike, bid, ask, iv, delta, oi, vol}
    Keeps out-of-the-money strikes within the moneyness band, expirations <= MAX_DTE.
    Returns {"YYYY-MM-DD": {"dte": n, "puts": [...], "calls": [...]}}
    """
    out = {}
    lo, hi = spot * (1 - MONEYNESS_BAND), spot * (1 + MONEYNESS_BAND)
    for r in rows:
        dte = (r["exp"] - today).days
        if dte <= 0 or dte > MAX_DTE:
            continue
        k = r["strike"]
        if k < lo or k > hi:
            continue
        is_call = r["cp"] == "C"
        # OTM only: puts below spot, calls above spot (keep one ATM strike each side)
        if is_call and k < spot * 0.995:
            continue
        if not is_call and k > spot * 1.005:
            continue
        if (r.get("oi") or 0) < MIN_OPEN_INTEREST:
            continue
        bid = clean(r.get("bid"), 2) or 0.0
        if bid <= 0:
            continue
        delta = clean(r.get("delta"), 3)
        if delta is None:
            delta = clean(bs_delta(spot, k, dte, r.get("iv"), is_call), 3)
        key = r["exp"].isoformat()
        bucket = out.setdefault(key, {"dte": dte, "puts": [], "calls": []})
        # compact row: [strike, bid, ask, iv, delta, openInterest]
        bucket["calls" if is_call else "puts"].append([
            clean(k, 2), bid, clean(r.get("ask"), 2), clean(r.get("iv"), 3),
            delta, int(r.get("oi") or 0),
        ])
    for b in out.values():
        b["puts"].sort(key=lambda x: -x[0])   # nearest to spot first, walking down
        b["calls"].sort(key=lambda x: x[0])   # nearest to spot first, walking up
    return out


def atm_iv(chain, target_dte=30):
    """Approximate 30-day at-the-money IV from the trimmed chain."""
    best = None
    for exp, b in chain.items():
        if not b["puts"] or not b["calls"]:
            continue
        cand = [b["puts"][0][3], b["calls"][0][3]]
        cand = [c for c in cand if c]
        if not cand:
            continue
        iv = sum(cand) / len(cand)
        score = abs(b["dte"] - target_dte)
        if best is None or score < best[0]:
            best = (score, iv)
    return best[1] if best else None


def realized_vol(closes, window):
    if len(closes) < window + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    w = rets[-window:]
    mean = sum(w) / len(w)
    var = sum((x - mean) ** 2 for x in w) / (len(w) - 1)
    return math.sqrt(var) * math.sqrt(252)


def hv_series(closes, window=30):
    """Rolling realized vol series, for percentile context."""
    out = []
    for i in range(window + 1, len(closes) + 1):
        v = realized_vol(closes[:i], window)
        if v:
            out.append(v)
    return out


def percentile_rank(value, series):
    if value is None or not series:
        return None
    below = sum(1 for s in series if s < value)
    return round(100.0 * below / len(series))


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

def fetch_cboe_chain(sym):
    """CBOE public delayed quotes. Returns (spot, rows) or raises."""
    cboe_sym = sym.replace(".", "")
    req = urllib.request.Request(CBOE_URL.format(sym=cboe_sym), headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    data = payload["data"]
    spot = float(data.get("current_price") or data.get("close") or 0)
    rows = []
    for o in data.get("options", []):
        parsed = parse_occ(o.get("option", ""))
        if not parsed:
            continue
        _, exp, cp, strike = parsed
        rows.append({
            "exp": exp, "cp": cp, "strike": strike,
            "bid": o.get("bid"), "ask": o.get("ask"), "iv": o.get("iv"),
            "delta": o.get("delta"), "oi": o.get("open_interest"),
            "vol": o.get("volume"),
        })
    if spot <= 0 or not rows:
        raise RuntimeError("CBOE returned no usable chain")
    return spot, rows


def fetch_yahoo_chain(tk, spot, today):
    """yfinance fallback. tk is a yfinance.Ticker."""
    rows = []
    for exp_str in tk.options:
        exp = dt.date.fromisoformat(exp_str)
        if (exp - today).days > MAX_DTE:
            continue
        ch = tk.option_chain(exp_str)
        for cp, df in (("C", ch.calls), ("P", ch.puts)):
            for rec in df.to_dict("records"):
                rows.append({
                    "exp": exp, "cp": cp, "strike": float(rec["strike"]),
                    "bid": rec.get("bid"), "ask": rec.get("ask"),
                    "iv": rec.get("impliedVolatility"), "delta": None,
                    "oi": rec.get("openInterest"), "vol": rec.get("volume"),
                })
        time.sleep(0.3)
    if not rows:
        raise RuntimeError("Yahoo returned no chain")
    return rows


def fetch_context(sym, today):
    """Price history, earnings, dividends, news via yfinance. Every piece optional."""
    import yfinance as yf
    tk = yf.Ticker(sym)
    ctx = {"name": sym, "sector": None, "spot": None, "prevClose": None,
           "high52": None, "low52": None, "closes": [], "earnings": None,
           "exDiv": None, "divAmount": None, "divYield": None, "news": []}

    try:
        hist = tk.history(period="1y", auto_adjust=False)
        closes = [float(c) for c in hist["Close"].tolist() if c == c]
        ctx["closes"] = closes
        if closes:
            ctx["spot"] = closes[-1]
            ctx["prevClose"] = closes[-2] if len(closes) > 1 else closes[-1]
            ctx["high52"] = max(closes)
            ctx["low52"] = min(closes)
    except Exception as e:  # noqa: BLE001
        log(f"  {sym}: history failed: {e}")

    try:
        fi = tk.fast_info
        lp = getattr(fi, "last_price", None)
        if lp:
            ctx["spot"] = float(lp)
    except Exception:  # noqa: BLE001
        pass

    try:
        info = tk.get_info() or {}
        ctx["name"] = info.get("shortName") or info.get("longName") or sym
        ctx["sector"] = info.get("sector")
        if info.get("dividendYield") is not None:
            dy = float(info["dividendYield"])
            ctx["divYield"] = dy / 100.0 if dy > 1 else dy  # yfinance changed units over time
        exd = info.get("exDividendDate")
        if exd:
            ctx["exDiv"] = dt.datetime.utcfromtimestamp(int(exd)).date().isoformat()
        if info.get("dividendRate"):
            ctx["divAmount"] = float(info["dividendRate"]) / 4.0
    except Exception as e:  # noqa: BLE001
        log(f"  {sym}: info failed: {e}")

    try:
        cal = tk.calendar or {}
        eds = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if eds:
            eds = eds if isinstance(eds, list) else [eds]
            future = sorted(d for d in eds if hasattr(d, "isoformat") and d >= today)
            if future:
                ctx["earnings"] = future[0].isoformat()
            elif eds:
                ctx["earnings"] = sorted(eds)[0].isoformat()
        if isinstance(cal, dict) and cal.get("Ex-Dividend Date") and not ctx["exDiv"]:
            ctx["exDiv"] = cal["Ex-Dividend Date"].isoformat()
    except Exception as e:  # noqa: BLE001
        log(f"  {sym}: calendar failed: {e}")

    try:
        items = []
        for n in (tk.news or [])[:NEWS_ITEMS]:
            c = n.get("content", n)
            title = c.get("title")
            link = (c.get("canonicalUrl") or {}).get("url") or c.get("link")
            pub = (c.get("provider") or {}).get("displayName") or n.get("publisher")
            when = c.get("pubDate") or c.get("displayTime")
            if not when and n.get("providerPublishTime"):
                when = dt.datetime.utcfromtimestamp(n["providerPublishTime"]).isoformat()
            if title and link:
                items.append({"title": title, "url": link, "source": pub, "date": when})
        ctx["news"] = items
    except Exception as e:  # noqa: BLE001
        log(f"  {sym}: news failed: {e}")

    return tk, ctx


# ---------------------------------------------------------------------------
# Demo data (no network)
# ---------------------------------------------------------------------------

def demo_ticker(sym, today, rng):
    base_iv = rng.uniform(0.18, 0.75)
    spot = rng.choice([rng.uniform(8, 40), rng.uniform(40, 250), rng.uniform(250, 900)])
    closes, p = [], spot * rng.uniform(0.75, 1.15)
    for _ in range(252):
        p *= math.exp(rng.gauss(0, base_iv / math.sqrt(252)))
        closes.append(p)
    scale = spot / closes[-1]
    closes = [c * scale for c in closes]
    rows = []
    # weekly expirations for 8 weeks, then monthlies
    fridays = []
    d = today + dt.timedelta(days=(4 - today.weekday()) % 7 or 7)
    while (d - today).days <= MAX_DTE:
        fridays.append(d)
        d += dt.timedelta(days=7)
    keep = fridays[:8] + [f for f in fridays[8:] if 15 <= f.day <= 21]
    step = max(0.5, round(spot * 0.025 / 0.5) * 0.5) if spot < 100 else round(spot * 0.02)
    for exp in keep:
        dte = (exp - today).days
        t = dte / 365
        for i in range(-16, 17):
            k = round(spot + i * step, 2)
            if k <= 0:
                continue
            skew = 1 + 0.35 * max(0, (spot - k) / spot) + 0.1 * max(0, (k - spot) / spot)
            iv = base_iv * skew * rng.uniform(0.97, 1.03)
            for cp in ("C", "P"):
                is_call = cp == "C"
                delta = bs_delta(spot, k, dte, iv, is_call)
                d1 = (math.log(spot / k) + (0.04 + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))
                d2 = d1 - iv * math.sqrt(t)
                if is_call:
                    price = spot * norm_cdf(d1) - k * math.exp(-0.04 * t) * norm_cdf(d2)
                else:
                    price = k * math.exp(-0.04 * t) * norm_cdf(-d2) - spot * norm_cdf(-d1)
                mid = max(price, 0.01)
                spread = max(0.01, mid * rng.uniform(0.02, 0.12))
                rows.append({"exp": exp, "cp": cp, "strike": k, "bid": round(max(mid - spread / 2, 0), 2),
                             "ask": round(mid + spread / 2, 2), "iv": iv, "delta": delta,
                             "oi": int(rng.expovariate(1 / 800)), "vol": int(rng.expovariate(1 / 200))})
    earn = today + dt.timedelta(days=rng.randint(3, 80))
    has_div = rng.random() < 0.55
    ctx = {"name": f"{sym} Inc. (demo)", "sector": rng.choice(["Technology", "Financials", "Energy", "Consumer", "Healthcare"]),
           "spot": spot, "prevClose": spot / math.exp(rng.gauss(0, 0.015)), "high52": max(closes), "low52": min(closes),
           "closes": closes, "earnings": earn.isoformat(),
           "exDiv": (today + dt.timedelta(days=rng.randint(2, 70))).isoformat() if has_div else None,
           "divAmount": round(spot * rng.uniform(0.002, 0.01), 2) if has_div else None,
           "divYield": rng.uniform(0.008, 0.04) if has_div else None,
           "news": [{"title": f"{sym} demo headline {i + 1}: analysts weigh outlook ahead of results",
                     "url": "https://example.com", "source": "Demo Wire",
                     "date": (today - dt.timedelta(days=i)).isoformat()} for i in range(4)]}
    return spot, rows, ctx


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_ticker(sym, today, demo, rng):
    if demo:
        spot, rows, ctx = demo_ticker(sym, today, rng)
        source = "demo"
    else:
        tk, ctx = fetch_context(sym, today)
        spot, rows, source = None, None, None
        try:
            spot, rows = fetch_cboe_chain(sym)
            source = "cboe"
        except Exception as e:  # noqa: BLE001
            log(f"  {sym}: CBOE failed ({e}); trying Yahoo")
            spot = ctx["spot"]
            if not spot:
                raise RuntimeError("no spot price available")
            rows = fetch_yahoo_chain(tk, spot, today)
            source = "yahoo"
        if ctx["spot"] and abs(ctx["spot"] - spot) / spot > 0.05:
            log(f"  {sym}: spot mismatch cboe={spot:.2f} yahoo={ctx['spot']:.2f}; using chain source")
        ctx["spot"] = spot

    chain = trim_chain(rows, spot, today)
    closes = ctx.pop("closes", [])
    hv30 = realized_vol(closes, 30) if closes else None
    hv_hist = hv_series(closes, 30) if closes else []
    iv30 = atm_iv(chain)

    return {
        "symbol": sym,
        "name": ctx["name"],
        "sector": ctx["sector"],
        "spot": clean(spot, 2),
        "prevClose": clean(ctx["prevClose"], 2),
        "high52": clean(ctx["high52"], 2),
        "low52": clean(ctx["low52"], 2),
        "iv30": clean(iv30, 3),
        "hv30": clean(hv30, 3),
        # Where today's 30d IV sits vs. the past year of realized 30d vol (0-100)
        "ivVsHvRank": percentile_rank(iv30, hv_hist),
        "earnings": ctx["earnings"],
        "exDiv": ctx["exDiv"],
        "divAmount": clean(ctx["divAmount"], 2),
        "divYield": clean(ctx["divYield"], 4),
        "news": ctx["news"],
        "source": source,
        "chain": chain,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--basket", default="basket.txt")
    ap.add_argument("--out", default="docs/data.json")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    today = dt.date.today()
    rng = random.Random(args.seed)
    symbols = read_basket(args.basket)
    log(f"Building {len(symbols)} tickers ({'demo' if args.demo else 'live'})")

    tickers, errors = [], []
    for sym in symbols:
        log(f"- {sym}")
        try:
            tickers.append(build_ticker(sym, today, args.demo, rng))
        except Exception as e:  # noqa: BLE001
            log(f"  {sym}: FAILED: {e}")
            errors.append({"symbol": sym, "error": str(e)})
        if not args.demo:
            time.sleep(0.8)  # be polite to free endpoints

    out = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "asOf": today.isoformat(),
        "demo": args.demo,
        "settings": {"maxDte": MAX_DTE, "moneynessBand": MONEYNESS_BAND},
        "tickers": tickers,
        "errors": errors,
    }
    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    size = os.path.getsize(args.out)
    log(f"Wrote {args.out} ({size / 1024:.0f} KB), {len(tickers)} ok, {len(errors)} failed")
    if not tickers:
        sys.exit(1)


if __name__ == "__main__":
    main()
