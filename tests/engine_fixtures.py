"""Synthetic charts for the catalyst / order block / brief tests, in Yahoo's chart JSON shape.

One stock (KEC by default): 25 quiet sessions of 5-minute bars, then a session with a textbook bullish
order block: a swing high at 09:35, a bearish candle at 10:00 (the OB, 99.70-100.30), a three-candle
impulse to 102.30 that closes above the swing high and leaves a fair value gap, a pull-back, and at 10:35
a stop-hunt wick to 99.50 that closes back at 100.80 on about 5x volume (a sweep and reclaim). The signal
bar closes at 10:40. Daily bars: 300 sessions of a gentle random walk ending the day before.
"""

import datetime
import json
import os
import random

IST = datetime.timedelta(hours=5, minutes=30)
EVENT_DAY = datetime.date(2026, 10, 23)          # a Friday


def _sessions_before(day, n):
    out, d = [], day
    while len(out) < n:
        d -= datetime.timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in ("2026-10-20", "2026-10-02"):
            out.append(d)
    return sorted(out)


def _ts(day, hh, mm):
    local = datetime.datetime.combine(day, datetime.time(hh, mm))
    return int((local - IST).replace(tzinfo=datetime.timezone.utc).timestamp())


def _chart(symbol, rows, gran="5m"):
    return {"chart": {"result": [{"meta": {"symbol": symbol, "dataGranularity": gran, "currency": "INR",
                                           "exchangeName": "NSI", "regularMarketPrice": rows[-1][4]},
                                  "timestamp": [r[0] for r in rows],
                                  "indicators": {"quote": [{"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                                                            "low": [r[3] for r in rows], "close": [r[4] for r in rows],
                                                            "volume": [r[5] for r in rows]}]}}], "error": None}}


def event_day_bars(day=EVENT_DAY):
    """(hh, mm, o, h, l, c, v) for the morning of the event day, up to the 10:35 sweep bar."""
    bars = []
    t = [(9, 15 + 5 * i) if 15 + 5 * i < 60 else (10, 5 * i - 45) for i in range(9)]
    flat = [100.0, 100.05, 99.95, 100.0, 100.05, 100.0, 99.95, 100.0, 100.05]
    for i, (hh, mm) in enumerate(t):
        o = flat[i]
        c = flat[(i + 1) % len(flat)]
        h = max(o, c) + 0.15
        lo = min(o, c) - 0.15
        if (hh, mm) == (9, 35):
            h = 100.6                                 # the swing high the impulse must break
        bars.append((hh, mm, o, h, lo, c, 1000))
    bars += [
        (10, 0, 100.2, 100.3, 99.7, 99.8, 1100),      # the order block: the last bearish candle
        (10, 5, 99.8, 101.0, 99.8, 100.9, 1500),      # impulse, closes above the 100.6 swing high (BOS)
        (10, 10, 100.9, 101.9, 100.8, 101.8, 1500),
        (10, 15, 101.8, 102.3, 101.5, 102.2, 1500),   # 10:05 high 101.0 < 10:15 low 101.5: a fair value gap
        (10, 20, 102.2, 102.25, 101.5, 101.6, 1000),
        (10, 25, 101.6, 101.7, 100.9, 101.0, 1000),
        (10, 30, 101.0, 101.05, 100.4, 100.5, 1000),
        (10, 35, 100.5, 100.9, 99.5, 100.8, 6000),    # sweep under 99.70 and reclaim, ~5x volume
    ]
    return bars


def five_minute_chart(symbol="KEC", day=EVENT_DAY, sessions=25, seed=3):
    rnd = random.Random(seed)
    rows, p = [], 100.0
    for d in _sessions_before(day, sessions):
        for k in range(75):
            m = 9 * 60 + 15 + 5 * k
            o = p
            c = round(o * (1 + rnd.gauss(0, 0.0015)), 2)
            h = round(max(o, c) + abs(rnd.gauss(0, 0.08)) + 0.02, 2)
            lo = round(min(o, c) - abs(rnd.gauss(0, 0.08)) - 0.02, 2)
            rows.append((_ts(d, m // 60, m % 60), o, h, lo, c, 1000 + rnd.randint(0, 300)))
            p = c
        p = 100.0 + (p - 100.0) * 0.5                 # keep the walk near 100
    for hh, mm, o, h, lo, c, v in event_day_bars(day):
        rows.append((_ts(day, hh, mm), o, h, lo, c, v))
    return _chart(symbol + ".NS", rows)


def daily_bars(day=EVENT_DAY, n=300, seed=5, drift=0.0008):
    """Bars (dicts) for the n sessions before `day`, a gentle random walk to about 100."""
    rnd = random.Random(seed)
    days = _sessions_before(day, n)
    out, p = [], 80.0
    for d in days:
        o = p * (1 + rnd.gauss(0, 0.004))
        c = o * (1 + rnd.gauss(drift, 0.012))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.004)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, 0.004)))
        out.append({"date": d.isoformat(), "time": "", "ts": _ts(d, 9, 15), "o": round(o, 2), "h": round(h, 2),
                    "l": round(lo, 2), "c": round(c, 2), "v": 500000.0})
        p = c
    scale = 100.0 / out[-1]["c"]
    for b in out:
        for k in ("o", "h", "l", "c"):
            b[k] = round(b[k] * scale, 2)
    return out


def write(dirpath, symbol="KEC", day=EVENT_DAY):
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, "%s.NS_5m.json" % symbol), "w") as fh:
        json.dump(five_minute_chart(symbol, day), fh)
    with open(os.path.join(dirpath, "%s.NS_1d.json" % symbol), "w") as fh:
        json.dump(daily_bars(day), fh)
    return dirpath
