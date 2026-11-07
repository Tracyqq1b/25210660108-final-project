"""Market data feed: East Money quote API download + local CSV cache
with as-of cutoff.

Real end-of-day bars are fetched once over the network from the East
Money push2his kline API (a publicly queryable market-data database),
then replay and tests run against the local CSV cache only. Every read
path requires an explicit ``as_of`` date and never returns rows after
that date -- this is the single choke point for future-information
leakage control (see docs/02-design-note.md).
"""

import datetime as dt
import json
import os

import pandas as pd
import requests

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "cache")

# East Money secid prefix: 105 = NASDAQ, 106 = NYSE.
# NYSE Arca ETFs (SPY/TLT/GLD) are not covered, so the demo universe uses
# NASDAQ-listed instruments only.
SECTOR_ID = {"105": "NASDAQ", "106": "NYSE"}
SECID_OVERRIDE = {
    # "JNJ": "106.JNJ",  # example for a NYSE-listed symbol
}

_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"


class DataNotCachedError(RuntimeError):
    pass


def _cache_path(symbol, cache_dir=None):
    return os.path.join(cache_dir or CACHE_DIR, "%s.csv" % symbol.upper())


def _secid(symbol):
    return SECID_OVERRIDE.get(symbol.upper(), "105." + symbol.upper())


def refresh_cache(symbols, start, end, cache_dir=None):
    """Download daily OHLCV bars from East Money into the local cache.

    ``start``/``end`` are 'YYYYMMDD' or 'YYYY-MM-DD' strings, inclusive.
    Network access happens only here; replay and tests read the cache only.
    """
    cache_dir = cache_dir or CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    written = []
    for symbol in symbols:
        params = {
            "secid": _secid(symbol),
            "fields1": "f1,f2,f3",
            "fields2": "f51,f52,f53,f54,f55,f56",
            "klt": "101",   # daily bars
            "fqt": "1",     # forward-adjusted
            "beg": _norm_date(start).replace("-", ""),
            "end": _norm_date(end).replace("-", ""),
        }
        resp = requests.get(_KLINE_URL, params=params, timeout=20)
        resp.raise_for_status()
        payload = json.loads(resp.text)
        data = payload.get("data")
        if not data or not data.get("klines"):
            raise DataNotCachedError("no data returned for %s" % symbol)
        rows = []
        for line in data["klines"]:
            d, o, c, h, l, v = line.split(",")[:6]
            rows.append({"Date": d, "Open": float(o), "High": float(h),
                         "Low": float(l), "Close": float(c),
                         "Volume": float(v)})
        df = pd.DataFrame(rows).sort_values("Date").reset_index(drop=True)
        df.to_csv(_cache_path(symbol, cache_dir), index=False)
        written.append(symbol)
    return written


def load(symbol, as_of, cache_dir=None):
    """Return cached bars for ``symbol`` with date <= ``as_of``.

    Raises DataNotCachedError if the symbol is not cached. Never returns
    rows dated after ``as_of`` -- this is the leakage guard.
    """
    path = _cache_path(symbol, cache_dir)
    if not os.path.exists(path):
        raise DataNotCachedError(
            "%s not cached; run `cli.py refresh` first" % symbol)
    df = pd.read_csv(path, dtype={"Date": str})
    cutoff = _norm_date(as_of)
    return df[df["Date"] <= cutoff].reset_index(drop=True)


def price_on(symbol, day, cache_dir=None):
    """Close price on an exact trading day. Raises if ``day`` is not a
    trading day in the cache (prevents silently using stale prices)."""
    df = load(symbol, day, cache_dir)
    row = df[df["Date"] == _norm_date(day)]
    if row.empty:
        raise DataNotCachedError("%s has no bar on %s" % (symbol, day))
    return float(row["Close"].iloc[0])


def trading_days(start, end, cache_dir=None, calendar_symbol="QQQ"):
    """Trading days in [start, end] derived from the cached calendar symbol."""
    df = load(calendar_symbol, end, cache_dir)
    days = df[(df["Date"] >= _norm_date(start)) & (df["Date"] <= _norm_date(end))]
    return list(days["Date"])


def cache_range(symbol, cache_dir=None):
    """(first_date, last_date) available in cache for a symbol."""
    path = _cache_path(symbol, cache_dir)
    if not os.path.exists(path):
        raise DataNotCachedError("%s not cached" % symbol)
    df = pd.read_csv(path, dtype={"Date": str})
    return df["Date"].iloc[0], df["Date"].iloc[-1]


def _norm_date(day):
    if isinstance(day, (dt.date, dt.datetime)):
        return day.strftime("%Y-%m-%d")
    return str(day)[:10]
