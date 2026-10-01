"""Explicit compatible AkShare daily-source registry; no network/cache on import."""
from __future__ import annotations

from services.market_fallback import MarketSource


def exchange_symbol(symbol: str) -> str:
    value = str(symbol).strip()
    if "." in value:
        code, exchange = value.split(".", 1)
        return exchange.lower()[:2] + code.zfill(6)
    code = value.zfill(6)
    prefix = "sh" if code.startswith(("5", "6", "900")) else (
        "bj" if code.startswith(("4", "8", "9")) else "sz"
    )
    return prefix + code


def raw_security_sources(ak, symbol: str, start_date: str, end_date: str, *, fund=False, tencent_first=False):
    """Unadjusted security history only: Tencent/Sina adjusted bases differ."""
    full = exchange_symbol(symbol)
    code = full[2:]
    kwargs = dict(start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adjust="")
    em = []
    if fund:
        for api in ("fund_etf_hist_em", "fund_lof_hist_em"):
            em.append(MarketSource(api, lambda api=api: getattr(ak, api)(symbol=code, period="daily", **kwargs), "akshare"))
    em.append(MarketSource("stock_zh_a_hist/东方财富", lambda: ak.stock_zh_a_hist(symbol=code, period="daily", timeout=12, **kwargs), "akshare"))
    tx = MarketSource("stock_zh_a_hist_tx/腾讯", lambda: ak.stock_zh_a_hist_tx(symbol=full, timeout=12, **kwargs), "akshare_tx")
    sina = [MarketSource("stock_zh_a_daily/新浪", lambda: ak.stock_zh_a_daily(symbol=full, **kwargs), "akshare_sina")]
    if fund:
        sina.insert(0, MarketSource("fund_etf_hist_sina/新浪", lambda: ak.fund_etf_hist_sina(symbol=full), "akshare_sina"))
    return ([tx] + em if tencent_first else em + [tx]) + sina


def futures_daily_sources(ak, contract: str):
    """Keep main-continuous series on Sina; exact contracts may use EastMoney."""
    import re

    sources = [MarketSource("futures_zh_daily_sina/新浪", lambda: ak.futures_zh_daily_sina(symbol=contract))]
    if re.fullmatch(r"[a-zA-Z]+0", contract):
        sources.append(MarketSource("futures_main_sina/新浪", lambda: ak.futures_main_sina(symbol=contract.upper())))
    elif re.fullmatch(r"[a-zA-Z]+\d{3,4}", contract):
        def eastmoney():
            # DCE codes are lower case, CFFEX codes upper case. Use the exact
            # vendor code rather than guessing its exchange or letter case.
            table = ak.futures_hist_table_em()
            codes = table["合约代码"].astype(str)
            matched = codes[codes.str.upper().eq(contract.upper())].drop_duplicates()
            if len(matched) != 1:
                raise ValueError(f"东方财富未唯一匹配具体合约 {contract}")
            return ak.futures_hist_em(symbol=matched.iloc[0], period="daily")

        sources.append(MarketSource("futures_hist_em/东方财富", eastmoney))
    return sources
