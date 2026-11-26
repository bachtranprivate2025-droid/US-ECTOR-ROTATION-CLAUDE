#!/usr/bin/env python3
"""Build data/rotation.json for the US Sector Rotation dashboard.

Sources
- Prices (daily + 5-minute pre-market): Yahoo Finance chart API
- ETF net flow (USD, daily): ETFIQ Open Data  https://etfiq.com/data/
- Next earnings date of the representative stock: TradingView scanner
- Perpetual availability: Bybit v5 instruments-info (falls back to a stored snapshot)

Only the Python standard library is used, so the GitHub Action needs no installs.
Run `python scripts/build_data.py --mock` to build from synthetic prices (offline test).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

# Rotation rule (same thresholds for completed sessions and pre-market)
ROT_FROM_MAX = -1.0   # source ETF performance must be <= -1%
ROT_TO_MIN = 0.5      # destination ETF performance must be >= +0.5%
ROT_SPREAD_MIN = 2.0  # spread >= 2 percentage points
PRE_FRESH_MIN = 20    # a pre-market price older than this is "delayed"
PRE_STALE_MIN = 60    # older than this: no RS
PRE_COVERAGE = 0.8    # >= 80% of ETFs must have fresh prices to confirm a pre rotation

ERRORS: list[str] = []


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- HTTP helpers
def http(url: str, data: bytes | None = None, headers: dict | None = None,
         timeout: int = 25, retries: int = 3) -> bytes:
    h = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404):
                break
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url[:90]} → {last}")


def http_json(url: str, **kw):
    return json.loads(http(url, **kw).decode("utf-8"))


# ---------------------------------------------------------------- live fetchers
class LiveFetcher:
    YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")

    def _yahoo(self, sym: str, params: dict) -> dict:
        q = urllib.parse.urlencode(params)
        last = None
        for host in self.YAHOO_HOSTS:
            try:
                j = http_json(f"https://{host}/v8/finance/chart/{urllib.parse.quote(sym)}?{q}")
                res = (j.get("chart") or {}).get("result") or []
                if res:
                    return res[0]
                last = (j.get("chart") or {}).get("error")
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"Yahoo {sym}: {last}")

    def daily(self, sym: str) -> list[dict]:
        res = self._yahoo(sym, {"range": "6mo", "interval": "1d", "includePrePost": "false",
                                "events": "div,split"})
        ts = res.get("timestamp") or []
        q = (res.get("indicators", {}).get("quote") or [{}])[0]
        adj = ((res.get("indicators", {}).get("adjclose") or [{}])[0]).get("adjclose") or [None] * len(ts)
        bars = []
        for i, t in enumerate(ts):
            c = q.get("close", [None])[i]
            if c is None:
                continue
            d = datetime.fromtimestamp(t, NY).date()
            bars.append({
                "date": d.isoformat(),
                "o": q.get("open", [None])[i] or c,
                "h": q.get("high", [None])[i] or c,
                "l": q.get("low", [None])[i] or c,
                "c": c,
                "adj": adj[i] if i < len(adj) and adj[i] is not None else c,
                "v": q.get("volume", [None])[i] or 0,
            })
        # one bar per date (Yahoo can repeat the live bar)
        dedup = {}
        for b in bars:
            dedup[b["date"]] = b
        return [dedup[k] for k in sorted(dedup)]

    def intraday(self, sym: str) -> list[dict]:
        res = self._yahoo(sym, {"range": "1d", "interval": "5m", "includePrePost": "true"})
        ts = res.get("timestamp") or []
        q = (res.get("indicators", {}).get("quote") or [{}])[0]
        out = []
        for i, t in enumerate(ts):
            c = q.get("close", [None])[i]
            if c is None:
                continue
            out.append({"ts": int(t), "c": c, "v": q.get("volume", [None])[i] or 0})
        return out

    def etfiq(self, sym: str) -> dict | None:
        try:
            return http_json(f"https://etfiq.com/data/flows/funds/{sym.lower()}.json")
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise

    def earnings(self, tv_symbols: list[str]) -> dict[str, str]:
        body = json.dumps({
            "symbols": {"tickers": tv_symbols, "query": {"types": []}},
            "columns": ["earnings_release_next_date", "earnings_release_date"],
        }).encode()
        j = http_json("https://scanner.tradingview.com/america/scan", data=body,
                      headers={"Content-Type": "application/json",
                               "Origin": "https://www.tradingview.com",
                               "Referer": "https://www.tradingview.com/"})
        today = datetime.now(NY).date()
        out = {}
        for row in j.get("data", []):
            vals = [v for v in (row.get("d") or []) if isinstance(v, (int, float)) and v > 0]
            dates = sorted({datetime.fromtimestamp(v, NY).date() for v in vals})
            future = [d for d in dates if d >= today]
            if future:
                out[row["s"]] = future[0].isoformat()
        return out

    def bybit(self) -> set[str]:
        symbols, cursor = set(), ""
        for _ in range(10):
            url = "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000"
            if cursor:
                url += "&cursor=" + urllib.parse.quote(cursor)
            j = http_json(url, retries=2)
            if j.get("retCode") != 0:
                raise RuntimeError(f"Bybit retCode {j.get('retCode')}: {j.get('retMsg')}")
            res = j.get("result") or {}
            for it in res.get("list", []):
                if it.get("status") == "Trading":
                    symbols.add(it.get("symbol"))
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
        return symbols


# ---------------------------------------------------------------- mock fetcher
class MockFetcher(LiveFetcher):
    """Synthetic prices so the pipeline and UI can be tested offline."""

    def __init__(self, now_ny: datetime):
        self.now = now_ny
        self.days = []
        d = now_ny.date()
        while len(self.days) < 126:
            if d.weekday() < 5:
                self.days.append(d)
            d -= timedelta(days=1)
        self.days.reverse()
        if now_ny.time() < datetime.strptime("09:30", "%H:%M").time():
            self.days = [x for x in self.days if x < now_ny.date()]

    def daily(self, sym):
        rnd = random.Random(sym)
        drift = rnd.uniform(-0.0015, 0.002)
        vol = rnd.uniform(0.007, 0.02)
        p, out = rnd.uniform(40, 600), []
        base_v = rnd.uniform(2e6, 4e7)
        for d in self.days:
            r = rnd.gauss(drift, vol)
            o = p
            c = p * (1 + r)
            h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
            l = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
            out.append({"date": d.isoformat(), "o": o, "h": h, "l": l, "c": c, "adj": c,
                        "v": base_v * rnd.uniform(0.5, 2.0)})
            p = c
        return out

    def intraday(self, sym):
        rnd = random.Random(sym + "pre")
        last_close = self.daily(sym)[-1]["c"]
        start = self.now.replace(hour=4, minute=0, second=0, microsecond=0)
        bars, t, p = [], start, last_close
        lag = rnd.choice([0, 0, 0, 5, 15, 30, 90])
        while t <= self.now - timedelta(minutes=lag):
            p *= 1 + rnd.gauss(0, 0.0012)
            bars.append({"ts": int(t.timestamp()), "c": p, "v": 0})
            t += timedelta(minutes=5)
        if sym == "XLB":
            return []
        return bars

    def etfiq(self, sym):
        rnd = random.Random(sym + "flow")
        if sym in ("JETS", "VNQ", "OIH", "VIS", "SLX", "GDX", "VGT"):
            return {"daily": [], "monthly": [{"m": "2026-10", "flow": rnd.gauss(0, 3e8), "src": "nport"}]}
        return {"daily": [{"d": d.isoformat(), "flow": rnd.gauss(0, 2.5e8)} for d in self.days[-14:-1]]}

    def earnings(self, tv_symbols):
        rnd = random.Random(1)
        return {s: (self.now.date() + timedelta(days=rnd.randint(1, 100))).isoformat() for s in tv_symbols}

    def bybit(self):
        raise RuntimeError("mock: Bybit offline")


# ---------------------------------------------------------------- math helpers
def pct(a: float, b: float) -> float | None:
    if a is None or b in (None, 0):
        return None
    return (a / b - 1.0) * 100.0


def rnd(x, n=4):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(x, n)


def ddmm(iso: str) -> str:
    return f"{iso[8:10]}/{iso[5:7]}"


def rotation_label(rs_d, rs_w, rs_m) -> str:
    if None in (rs_d, rs_w, rs_m):
        return "N/A"
    if rs_w > 0 and rs_m > 0:
        return "Leading mạnh" if rs_d > 0 else "Leading bền / nghỉ"
    if rs_w > 0 and rs_m <= 0:
        return "Improving ngắn hạn" if rs_d > 0 else "Trung tính"
    if rs_d > 0:  # rs_w <= 0
        return "Hồi kỹ thuật"
    return "Weakening" if rs_m > 0 else "Lagging"


def strength_from_rank(rank: int, n: int) -> int:
    if n <= 1:
        return 3
    p = (rank - 1) / (n - 1)
    return max(1, min(5, 6 - math.ceil(p * 5 - 1e-9)))


# ---------------------------------------------------------------- core metrics
def align(etf: list[dict], spy: list[dict]):
    m = {b["date"]: b for b in etf}
    rows = [(s, m[s["date"]]) for s in spy if s["date"] in m]
    return [r[1] for r in rows], [r[0] for r in rows]


def compute_etf(entry: dict, bars: list[dict], spy: list[dict], flows: dict | None,
                earnings: dict, bybit_etf: set, bybit_stock: dict) -> dict:
    e, s = align(bars, spy)
    n = len(e)
    if n < 45:
        raise RuntimeError(f"{entry['ticker']}: only {n} aligned sessions")
    ec = [b["adj"] for b in e]
    sc = [b["adj"] for b in s]

    def perf(arr, k, i=None):
        i = len(arr) - 1 if i is None else i
        return pct(arr[i], arr[i - k]) if i - k >= 0 else None

    out = {"ticker": entry["ticker"], "name": entry["name"],
           "stocks": " · ".join(entry["stocks"]) if entry["stocks"] else "N/A",
           "price": rnd(e[-1]["c"], 4)}
    for key, k in (("D", 1), ("3D", 3), ("W", 5), ("M", 21)):
        pe, ps = perf(ec, k), perf(sc, k)
        out["perf" + key] = rnd(pe)
        out["rs" + key] = rnd(pe - ps) if pe is not None and ps is not None else None

    last15 = list(range(n - 15, n))
    base = ec[last15[0]]
    out["perf15"] = [rnd(pct(ec[i], base)) for i in last15]
    out["dates15"] = [ddmm(e[i]["date"]) for i in last15]

    last10 = list(range(n - 10, n))
    out["dates10"] = [ddmm(e[i]["date"]) for i in last10]
    out["rs10"] = [rnd(perf(ec, 10, i) - perf(sc, 10, i)) for i in last10]

    rvol10, ad10 = [], []
    for i in last10:
        prev = [b["v"] for b in e[max(0, i - 20):i] if b["v"]]
        avg = sum(prev) / len(prev) if prev else 0
        rv = e[i]["v"] / avg if avg else None
        rvol10.append(rnd(rv))
        h, l, c = e[i]["h"], e[i]["l"], e[i]["c"]
        clv = ((c - l) - (h - c)) / (h - l) if h > l else 0.0
        ad10.append(rnd(clv * min(rv, 3.0)) if rv is not None else None)
    out["rvol10"], out["adProxy10"] = rvol10, ad10
    out["rvol"] = rvol10[-1]
    valid_ad = [x for x in ad10 if x is not None]
    out["adProxy10Total"] = rnd(sum(valid_ad)) if valid_ad else None
    out["adProxy3"] = rnd(sum(x for x in ad10[-3:] if x is not None)) if valid_ad else None

    r = out["rs10"]
    out["rsTrend"] = "up" if r[-1] is not None and r[-4] is not None and r[-1] >= r[-4] else "down"

    sd, sw, sm = out["rsD"], out["rsW"], out["rsM"]
    out["score"] = rnd(0.15 * sd + 0.35 * sw + 0.50 * sm) if None not in (sd, sw, sm) else None
    out["rotation"] = rotation_label(sd, sw, sm)

    # ETF net flow
    out.update(flow_block(flows))

    confirmed = (sw is not None and sw > 0 and out.get("etfFlowTotal10") is not None
                 and out["etfFlowTotal10"] > 0)
    out["catalystConfirmed"] = bool(confirmed)
    out["catalyst"] = ("RS + net flow xác nhận: " if confirmed else "Theo dõi: ") + entry["theme"]

    tv = entry.get("earnings")
    ed = earnings.get(tv) if tv else None
    if not ed and entry.get("earningsFallback"):
        if entry["earningsFallback"] >= datetime.now(NY).date().isoformat():
            ed = entry["earningsFallback"]
    out["earningsTicker"] = tv.split(":")[1] if tv else None
    out["earningsDate"] = ed
    out["earningsRaw"] = tv

    out["bybitEtfPerp"] = f"{entry['ticker']}USDT.P" if entry["ticker"] in bybit_etf else None
    out["bybitStockPerps"] = [
        {"ticker": t, "symbol": bybit_stock[t] + ".P"} for t in entry["stocks"] if t in bybit_stock
    ]
    out["_daily"] = e  # internal; removed before writing
    return out


def flow_block(flows: dict | None) -> dict:
    if flows is None:
        return {"etfFlowCoverage": "unavailable", "etfFlow10": [], "etfFlowDates10": [],
                "etfFlowTotal10": None, "etfFlow3": None, "etfFlowAsOf": None}
    daily = [x for x in (flows.get("daily") or []) if isinstance(x.get("flow"), (int, float)) and x.get("d")]
    daily.sort(key=lambda x: x["d"])
    fresh = daily and daily[-1]["d"] >= (datetime.now(NY).date() - timedelta(days=12)).isoformat()
    if fresh:
        last = daily[-10:]
        vals = [x["flow"] for x in last]
        return {"etfFlowCoverage": "daily", "etfFlow10": [round(v) for v in vals],
                "etfFlowDates10": [x["d"] for x in last], "etfFlowTotal10": round(sum(vals)),
                "etfFlow3": round(sum(vals[-3:])), "etfFlowAsOf": last[-1]["d"]}
    monthly = [x for x in (flows.get("monthly") or []) if isinstance(x.get("flow"), (int, float))]
    monthly.sort(key=lambda x: x.get("m", ""))
    if monthly:
        m = monthly[-1]
        return {"etfFlowCoverage": "monthly", "etfFlow10": [], "etfFlowDates10": [],
                "etfFlowTotal10": None, "etfFlow3": None, "etfFlowAsOf": m.get("m"),
                "etfFlowMonthly": round(m["flow"])}
    return {"etfFlowCoverage": "unavailable", "etfFlow10": [], "etfFlowDates10": [],
            "etfFlowTotal10": None, "etfFlow3": None, "etfFlowAsOf": None}


def rank_group(items: list[dict]) -> None:
    ok = [x for x in items if x.get("score") is not None]
    ok.sort(key=lambda x: x["score"], reverse=True)
    for i, x in enumerate(ok, 1):
        x["rank"] = i
        x["strength"] = strength_from_rank(i, len(ok))
    for x in items:
        x.setdefault("rank", None)
        x.setdefault("strength", None)


def rotation_pick(perfs: dict[str, float]) -> dict | None:
    vals = {k: v for k, v in perfs.items() if v is not None}
    if len(vals) < 2:
        return None
    frm = min(vals, key=vals.get)
    to = max(vals, key=vals.get)
    spread = vals[to] - vals[frm]
    return {"from": frm, "fromPerf": rnd(vals[frm]), "to": to, "toPerf": rnd(vals[to]),
            "spread": rnd(spread),
            "confirmed": vals[frm] <= ROT_FROM_MAX and vals[to] >= ROT_TO_MIN and spread >= ROT_SPREAD_MIN}


def daily_rotations(items: list[dict], sessions: int = 10) -> list[dict]:
    by_t = {x["ticker"]: {b["date"]: b["adj"] for b in x["_daily"]} for x in items}
    dates = sorted(set.intersection(*(set(v) for v in by_t.values())))
    out = []
    for i in range(len(dates) - 1, max(0, len(dates) - 1 - sessions), -1):
        d, p = dates[i], dates[i - 1]
        perfs = {t: pct(v[d], v[p]) for t, v in by_t.items()}
        r = rotation_pick(perfs)
        if r:
            r["date"] = d
            out.append(r)
    return out



# ---------------------------------------------------------------- sector leader map
def yahoo_sym(t: str) -> str:
    return t.replace(".", "-")


def stock_metrics(bars: list[dict], spy: list[dict]) -> dict | None:
    e, s = align(bars, spy)
    if len(e) < 25:
        return None
    ec, sc = [b["adj"] for b in e], [b["adj"] for b in s]
    out = {}
    for key, k in (("D", 1), ("W", 5), ("M", 21)):
        pe, ps = pct(ec[-1], ec[-1 - k]), pct(sc[-1], sc[-1 - k])
        out["perf" + key] = rnd(pe)
        out["rs" + key] = rnd(pe - ps) if pe is not None and ps is not None else None
    sd, sw, sm = out["rsD"], out["rsW"], out["rsM"]
    out["score"] = rnd(0.15 * sd + 0.35 * sw + 0.50 * sm) if None not in (sd, sw, sm) else None
    out["_close"] = e[-1]["c"]
    return out


def set_rating(rows: list[dict], key: str, out: str) -> None:
    ok = sorted([r for r in rows if r.get(key) is not None], key=lambda r: r[key])
    n = len(ok)
    for i, r in enumerate(ok):
        r[out] = int(round(1 + 98 * i / (n - 1))) if n > 1 else 50


def attach_leaders(items: list[dict], cfg_main: list[dict], stock_daily: dict, spy: list[dict],
                   bybit_stock: dict) -> None:
    """RS Rating 1-99 = percentile of the fast 1-day RS vs SPY across all leader stocks."""
    metrics = {}
    for t, bars in stock_daily.items():
        if bars:
            m = stock_metrics(bars, spy)
            if m and m["rsD"] is not None:
                metrics[t] = m
    set_rating(list(metrics.values()), "rsD", "rating")
    by_t = {e["ticker"]: e for e in cfg_main}
    for x in items:
        lst = [dict(t=t, **{k: v for k, v in metrics[t].items() if not k.startswith("_")})
               for t in by_t.get(x["ticker"], {}).get("leaders", []) if t in metrics]
        for r in lst:
            r["bybit"] = bybit_stock[r["t"]] + ".P" if r["t"] in bybit_stock else None
        lst.sort(key=lambda r: r["rating"], reverse=True)
        x["leaders"] = lst
        x["_leaderClose"] = {t: metrics[t]["_close"] for t in by_t.get(x["ticker"], {}).get("leaders", []) if t in metrics}

# ---------------------------------------------------------------- pre-market
def session_state(now: datetime, spy_last_date: str) -> str:
    if now.weekday() >= 5:
        return "completed"
    t = now.hour * 60 + now.minute
    if 4 * 60 <= t < 9 * 60 + 30:
        return "premarket"
    if 9 * 60 + 30 <= t < 16 * 60 and spy_last_date == now.date().isoformat():
        return "regular"
    return "completed"


def pre_quote(bars: list[dict], prev_close: float, now: datetime, spy_bars: list[dict] | None,
              spy_prev: float | None) -> dict:
    today = now.date()
    pre = [b for b in bars
           if datetime.fromtimestamp(b["ts"], NY).date() == today
           and datetime.fromtimestamp(b["ts"], NY).hour * 60 + datetime.fromtimestamp(b["ts"], NY).minute < 570]
    if not pre:
        return {"price": None, "perf": None, "rs": None, "volume": None, "asOf": None,
                "ageMinutes": None, "status": "none"}
    last = pre[-1]
    vol = sum(b["v"] or 0 for b in pre)
    age = (now.timestamp() - last["ts"]) / 60.0
    perf = pct(last["c"], prev_close)
    q = {"price": rnd(last["c"]), "perf": rnd(perf), "rs": None, "volume": vol or None,
         "asOf": datetime.fromtimestamp(last["ts"], timezone.utc).isoformat().replace("+00:00", "Z"),
         "ageMinutes": rnd(age, 1),
         "status": "live" if age <= PRE_FRESH_MIN else ("delayed" if age <= PRE_STALE_MIN else "stale")}
    if spy_bars is not None and spy_prev and q["status"] != "stale":
        same = [b for b in spy_bars if b["ts"] == last["ts"]]
        if not same:
            earlier = [b for b in spy_bars if 0 <= last["ts"] - b["ts"] <= 600]
            same = earlier[-1:] if earlier else []
        if same:
            q["rs"] = rnd(perf - pct(same[0]["c"], spy_prev))
            q["benchmarkAsOf"] = datetime.fromtimestamp(same[0]["ts"], timezone.utc).isoformat().replace("+00:00", "Z")
    return q


# ---------------------------------------------------------------- main build
def build(fetcher: LiveFetcher, now: datetime) -> dict:
    cfg = json.loads((ROOT / "config" / "universe.json").read_text("utf-8"))
    bench = cfg["benchmark"]
    groups = {"main": cfg["main"], "sub": cfg["sub"]}
    tickers = [bench] + [e["ticker"] for g in groups.values() for e in g]

    def safe(fn, sym):
        try:
            return fn(sym)
        except Exception as ex:  # noqa: BLE001
            ERRORS.append(f"{fn.__name__} {sym}: {ex}")
            return None

    with ThreadPoolExecutor(8) as pool:
        daily = dict(zip(tickers, pool.map(lambda t: safe(fetcher.daily, t), tickers)))
    if now.hour * 60 + now.minute < 570:  # before the 09:30 open: today's daily bar is not a session yet
        today = now.date().isoformat()
        daily = {k: ([b for b in v if b["date"] < today] if v else v) for k, v in daily.items()}
    spy = daily.get(bench)
    if not spy or len(spy) < 45:
        raise SystemExit("SPY daily data unavailable — aborting so the previous data stays online.")

    session = session_state(now, spy[-1]["date"])
    log(f"session={session} spyLast={spy[-1]['date']} errors={len(ERRORS)}")

    with ThreadPoolExecutor(8) as pool:
        flows = dict(zip(tickers[1:], pool.map(lambda t: safe(fetcher.etfiq, t), tickers[1:])))

    tv_syms = sorted({e["earnings"] for g in groups.values() for e in g if e.get("earnings")})
    try:
        earnings = fetcher.earnings(tv_syms)
    except Exception as ex:  # noqa: BLE001
        ERRORS.append(f"TradingView earnings: {ex}")
        earnings = {}

    snap = cfg["bybitSnapshot"]
    bybit_src = "Bybit v5 instruments-info"
    try:
        live = fetcher.bybit()
        bybit_etf = {t for t in snap["etf"] if f"{t}USDT" in live}
        bybit_stock = {t: s for t, s in snap["stock"].items() if s in live}
        bybit_ok = True
    except Exception as ex:  # noqa: BLE001
        log(f"Bybit fallback to snapshot: {ex}")
        bybit_etf, bybit_stock = set(snap["etf"]), dict(snap["stock"])
        bybit_src = f"Bybit snapshot {snap['asOf']}"
        bybit_ok = False

    result = {}
    for gname, entries in groups.items():
        items = []
        for entry in entries:
            bars = daily.get(entry["ticker"])
            if not bars:
                continue
            try:
                items.append(compute_etf(entry, bars, spy, flows.get(entry["ticker"]),
                                         earnings, bybit_etf, bybit_stock))
            except Exception as ex:  # noqa: BLE001
                ERRORS.append(f"compute {entry['ticker']}: {ex}")
        rank_group(items)
        result[gname] = items

    # ---- sector leader map (main sectors only)
    leader_t = sorted({t for e in groups["main"] for t in e.get("leaders", [])})
    with ThreadPoolExecutor(8) as pool:
        stock_daily = dict(zip(leader_t, pool.map(lambda t: safe(fetcher.daily, yahoo_sym(t)), leader_t)))
    if now.hour * 60 + now.minute < 570:
        today = now.date().isoformat()
        stock_daily = {k: ([b for b in v if b["date"] < today] if v else v) for k, v in stock_daily.items()}
    attach_leaders(result["main"], groups["main"], stock_daily, spy, bybit_stock)

    spy_c = [b["adj"] for b in spy]
    benchmark = {"ticker": bench, "price": rnd(spy[-1]["c"]),
                 "perfD": rnd(pct(spy_c[-1], spy_c[-2])), "perf3D": rnd(pct(spy_c[-1], spy_c[-4])),
                 "perfW": rnd(pct(spy_c[-1], spy_c[-6])), "perfM": rnd(pct(spy_c[-1], spy_c[-22])),
                 "perf15": [rnd(pct(c, spy_c[-15])) for c in spy_c[-15:]]}

    # ---- pre-market
    pre_block = {"date": now.date().isoformat(), "active": False, "benchmark": None,
                 "coverage": {"main": 0, "sub": 0}, "rotations": {"main": None, "sub": None},
                 "fresh": {"main": 0, "sub": 0}}
    if session == "premarket":
        all_t = [bench] + [x["ticker"] for g in result.values() for x in g]
        with ThreadPoolExecutor(8) as pool:
            intr = dict(zip(all_t, pool.map(lambda t: safe(fetcher.intraday, t), all_t)))
        spy_prev = spy[-1]["c"]
        spy_bars = intr.get(bench) or []
        bq = pre_quote(spy_bars, spy_prev, now, None, None)
        if bq["price"] is not None:
            bq["rs"] = 0.0
        pre_block["benchmark"] = bq
        pre_block["active"] = bq["price"] is not None
        for gname, items in result.items():
            for x in items:
                x["pre"] = pre_quote(intr.get(x["ticker"]) or [], x["_daily"][-1]["c"], now,
                                     spy_bars, spy_prev)
            with_rs = [x for x in items if x["pre"]["rs"] is not None]
            fresh = [x for x in items if x["pre"]["status"] == "live"]
            pre_block["coverage"][gname] = len(with_rs)
            pre_block["fresh"][gname] = len(fresh)
            pick = rotation_pick({x["ticker"]: x["pre"]["perf"] for x in with_rs})
            if pick:
                pick["confirmed"] = bool(pick["confirmed"] and len(fresh) >= PRE_COVERAGE * len(items))
            pre_block["rotations"][gname] = pick
        with ThreadPoolExecutor(8) as pool:
            s_intr = dict(zip(leader_t, pool.map(lambda t: safe(fetcher.intraday, yahoo_sym(t)), leader_t)))
        for x in result["main"]:
            closes = x.get("_leaderClose", {})
            for r in x.get("leaders", []):
                q = pre_quote(s_intr.get(r["t"]) or [], closes.get(r["t"]), now, None, None)
                spy_pre = bq["perf"]
                rs = rnd(q["perf"] - spy_pre) if q["perf"] is not None and spy_pre is not None else None
                r["pre"] = {"perf": q["perf"], "rs": rs, "status": q["status"]}
        set_rating([r["pre"] for x in result["main"] for r in x.get("leaders", [])], "rs", "rating")

    rot = {g: daily_rotations(items) for g, items in result.items() if items}

    cov = {"daily": 0, "monthly": 0, "unavailable": 0}
    for items in result.values():
        for x in items:
            cov[x["etfFlowCoverage"]] = cov.get(x["etfFlowCoverage"], 0) + 1
            x.pop("_daily", None)
            x.pop("_leaderClose", None)

    return {
        "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "asOf": spy[-1]["date"],
        "session": session,
        "source": "Yahoo Finance chart data + ETFIQ fund flows + TradingView earnings + Bybit instruments",
        "refreshSeconds": 60,
        "benchmark": benchmark,
        "preMarket": pre_block,
        "etfFlowSource": "ETFIQ Open Data",
        "etfFlowCoverage": cov,
        "bybitPerpSource": bybit_src,
        "bybitPerpCoverage": {"live": bybit_ok, "etf": len(bybit_etf), "stock": len(bybit_stock)},
        "main": result["main"],
        "sub": result["sub"],
        "dailyRotations": rot.get("main", []),
        "subDailyRotations": rot.get("sub", []),
        "errors": ERRORS[:50],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="synthetic data, no network")
    ap.add_argument("--now", help="override New York time, e.g. 2026-11-16T07:50")
    ap.add_argument("--out", default=str(ROOT / "data" / "rotation.json"))
    a = ap.parse_args()
    now = datetime.fromisoformat(a.now).replace(tzinfo=NY) if a.now else datetime.now(NY)
    fetcher = MockFetcher(now) if a.mock else LiveFetcher()
    data = build(fetcher, now)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    log(f"wrote {out} main={len(data['main'])} sub={len(data['sub'])} errors={len(data['errors'])}")
    for e in data["errors"][:15]:
        log("  ! " + e)


if __name__ == "__main__":
    main()
