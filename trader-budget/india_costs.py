"""What a round trip really costs on NSE, in rupees and as a % of the trade, before and after tax.

    charges(product, buy_value, sell_value)   statutory charges + brokerage, itemised (Rs)
    round_trip(product, price, qty, turnover_cr)   charges + slippage for one trade (Rs and %)
    cost_pct(product, order_value, turnover_cr)    the same as a % of the order value (simulators use it)
    cost_r(product, entry, stop, ...)          that cost in R (risk per share units)
    after_tax(product, gain_pct, slab_pct)     a gain after tax (STCG 20 % on delivery; business income
                                               at your slab for intraday and F&O)

Products: "delivery" (CNC, held overnight), "intraday" (MIS, squared off the same day), "futures".
Rates in force in 2026 (NSE, SEBI and state stamp-duty notifications); a % is of turnover:
                 STT                 stamp (buy)  NSE transaction  SEBI fee   brokerage (discount broker)
  delivery       0.1 % buy + sell    0.015 %      0.00297 %        0.0001 %   0 (DP Rs 15.93 a sell)
  intraday       0.025 % sell        0.003 %      0.00297 %        0.0001 %   0.03 % or Rs 20 an order
  futures        0.02 % sell         0.002 %      0.00173 %        0.0001 %   0.03 % or Rs 20 an order
GST 18 % on brokerage + transaction + SEBI fees. Slippage per side by liquidity (median daily turnover):
Rs 100 cr+ 0.02 %, 25 cr+ 0.05 %, 5 cr+ 0.10 %, less 0.25 %. Change any of it in trader/config.json
{"costs": {"intraday": {"brokerage_cap": 0}, "slippage_pct": [[100, 0.02], ...], "tax_slab_pct": 30}};
your broker's contract note is the final word.

The same copy lives in trader-timemachine and trader-budget (a repo test keeps them identical).
"""

import copy

RATES = {
    "delivery": {"stt_buy": 0.1, "stt_sell": 0.1, "stamp_buy": 0.015, "txn": 0.00297, "sebi": 0.0001,
                 "brokerage_pct": 0.0, "brokerage_cap": 0.0, "dp_per_sell": 15.93},
    "intraday": {"stt_buy": 0.0, "stt_sell": 0.025, "stamp_buy": 0.003, "txn": 0.00297, "sebi": 0.0001,
                 "brokerage_pct": 0.03, "brokerage_cap": 20.0, "dp_per_sell": 0.0},
    "futures": {"stt_buy": 0.0, "stt_sell": 0.02, "stamp_buy": 0.002, "txn": 0.00173, "sebi": 0.0001,
                "brokerage_pct": 0.03, "brokerage_cap": 20.0, "dp_per_sell": 0.0},
}
GST_PCT = 18.0
SLIPPAGE = ((100.0, 0.02), (25.0, 0.05), (5.0, 0.10), (0.0, 0.25))   # (turnover Rs cr at least, % per side)
STCG_PCT = 20.0          # listed equity held 12 months or less (Finance Act 2024)
TAX_SLAB_PCT = 30.0      # intraday (speculative) and F&O (non-speculative) profits: your income-tax slab
REF_ORDER = 100000.0     # Rs: the order size a % is quoted for when the quantity is not known yet


def rates_from(cfg=None):
    """RATES with the config's "costs" block laid over it."""
    r = copy.deepcopy(RATES)
    c = (cfg or {}).get("costs") or {}
    for prod in r:
        r[prod].update({k: float(v) for k, v in (c.get(prod) or {}).items() if isinstance(v, (int, float))})
    slip = c.get("slippage_pct")
    slip = tuple(sorted(((float(a), float(b)) for a, b in slip), reverse=True)) if slip else SLIPPAGE
    return r, slip, float(c.get("gst_pct", GST_PCT)), float(c.get("tax_slab_pct", TAX_SLAB_PCT))


def slippage_pct(turnover_cr=None, cfg=None):
    """% per side for a stock trading `turnover_cr` Rs crore a day (unknown -> the 5-25 cr tier)."""
    _, slip, _, _ = rates_from(cfg)
    t = 5.0 if turnover_cr is None else float(turnover_cr)
    for floor, pct in slip:
        if t >= floor:
            return pct
    return slip[-1][1]


def _brokerage(value, rt):
    b = value * rt["brokerage_pct"] / 100.0
    return min(b, rt["brokerage_cap"]) if rt["brokerage_cap"] else b


def charges(product, buy_value, sell_value, cfg=None):
    """Itemised statutory charges and brokerage for one buy and one sell (Rs)."""
    r, _, gst_pct, _ = rates_from(cfg)
    rt = r[product]
    stt = buy_value * rt["stt_buy"] / 100.0 + sell_value * rt["stt_sell"] / 100.0
    stamp = buy_value * rt["stamp_buy"] / 100.0
    txn = (buy_value + sell_value) * rt["txn"] / 100.0
    sebi = (buy_value + sell_value) * rt["sebi"] / 100.0
    brk = _brokerage(buy_value, rt) + _brokerage(sell_value, rt)
    gst = (brk + txn + sebi) * gst_pct / 100.0
    dp = rt["dp_per_sell"] if sell_value else 0.0
    out = {"stt": stt, "stamp": stamp, "exchange": txn, "sebi": sebi, "brokerage": brk, "gst": gst, "dp": dp}
    out = {k: round(v, 2) for k, v in out.items()}
    out["total"] = round(sum(v for k, v in out.items()), 2)
    return out


def round_trip(product, price, qty, turnover_cr=None, cfg=None, exit_price=None):
    """Charges + slippage (both sides) for qty shares at price (exit at exit_price, default the same)."""
    buy = float(price) * float(qty)
    sell = float(exit_price or price) * float(qty)
    ch = charges(product, buy, sell, cfg)
    slip = (buy + sell) * slippage_pct(turnover_cr, cfg) / 100.0
    total = ch["total"] + slip
    return {"product": product, "charges": ch, "slippage": round(slip, 2), "total": round(total, 2),
            "pct": round(total / buy * 100.0, 4) if buy else None}


def cost_pct(product, order_value=REF_ORDER, turnover_cr=None, cfg=None):
    """Round-trip cost (charges + slippage) as a % of the order value."""
    return round_trip(product, order_value, 1, turnover_cr, cfg)["pct"]


def cost_r(product, entry, stop, order_value=REF_ORDER, turnover_cr=None, cfg=None):
    """The round-trip cost in R: cost % x entry / risk per share."""
    risk = abs(float(entry) - float(stop))
    if not risk:
        return None
    return round(cost_pct(product, order_value, turnover_cr, cfg) / 100.0 * float(entry) / risk, 4)


def after_tax(product, gain_pct, cfg=None):
    """A gain after tax (losses are left as they are: set-off rules depend on your other income)."""
    _, _, _, slab = rates_from(cfg)
    if gain_pct is None or gain_pct <= 0:
        return gain_pct
    rate = STCG_PCT if product == "delivery" else slab
    return round(gain_pct * (1 - rate / 100.0), 4)
