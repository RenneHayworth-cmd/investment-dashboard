from __future__ import annotations

from threading import BoundedSemaphore

YAHOO_CHART_HOSTS = (
    "query1.finance.yahoo.com",
    "query2.finance.yahoo.com",
)
YAHOO_REQUEST_GATE = BoundedSemaphore(2)


INDEX_CONFIG = {
    "上证指数": {
        "source": "akshare_cn",
        "code": "000001",
        "market": "sh",
        "market_group": "A股",
        "tickflow_symbol": "000001.SH",
    },
    "创业板指": {
        "source": "akshare_cn",
        "code": "399006",
        "market": "sz",
        "market_group": "A股",
        "tickflow_symbol": "399006.SZ",
    },
    "沪深300": {
        "source": "akshare_cn",
        "code": "000300",
        "market": "sh",
        "market_group": "A股",
        "tickflow_symbol": "000300.SH",
    },
    "中证500": {
        "source": "akshare_cn",
        "code": "000905",
        "market": "sh",
        "market_group": "A股",
        "tickflow_symbol": "000905.SH",
    },
    "中证1000": {
        "source": "akshare_cn",
        "code": "000852",
        "market": "sh",
        "market_group": "A股",
        "tickflow_symbol": "000852.SH",
    },
    "中证2000": {
        "source": "akshare_cn",
        "code": "932000",
        "market": "sh",
        "market_group": "A股",
        "tickflow_symbol": "932000.SH",
        "eastmoney_quote_secid": "2.932000",
    },
    "微盘股": {
        "source": "eastmoney_kline",
        "code": "90.BK1158",
        "display_symbol": "BK1158",
        "market_group": "A股",
        "fqt": "1",
        "akshare_board_symbol": "BK1158",
        "require_current_quote": True,
    },
    "科创50": {
        "source": "akshare_cn",
        "code": "000688",
        "market": "sh",
        "market_group": "A股",
        "tickflow_symbol": "000688.SH",
    },
    "中证红利低波": {
        "source": "akshare_csindex",
        "code": "H30269",
        "market_group": "A股",
    },
    "国证自由现金流": {
        "source": "akshare_cni",
        "code": "980092",
        "market": "sz",
        "market_group": "A股",
        "tickflow_symbol": "980092.SZ",
    },
    "恒生科技": {
        "source": "akshare_hk",
        "code": "HSTECH",
        "display_symbol": "HSTECH",
        "market_group": "港股",
        "eastmoney_quote_secid": "124.HSTECH",
        "require_current_quote": True,
    },
    "恒生港股通高息低波": {
        "source": "eastmoney_kline",
        "code": "124.HSHYLV",
        "display_symbol": "HSHYLV",
        "market_group": "港股",
        "akshare_hk_em_symbol": "HSHYLV",
        "sina_hk_symbol": "HSHYLV",
        "hsi_official_series": "hshylv",
        "mx_query_name": "恒生港股通高股息低波动指数HSHYLV",
        "mx_expected_code": "HSHYLV.HI",
        "optional": True,
        "require_current_quote": True,
    },
    "标普500": {
        "source": "akshare_us",
        "code": ".INX",
        "tickflow_symbol": ".INX.US",
        "yahoo_symbol": "^GSPC",
        "display_symbol": "SPX",
        "market_group": "美股",
        "require_current_quote": True,
    },
    "纳斯达克综合": {
        "source": "akshare_us",
        "code": ".IXIC",
        "yahoo_symbol": "^IXIC",
        "display_symbol": "IXIC",
        "market_group": "美股",
        "require_current_quote": True,
    },
    "纳斯达克100": {
        "source": "akshare_us",
        "code": ".NDX",
        "yahoo_symbol": "^NDX",
        "display_symbol": "NDX",
        "market_group": "美股",
        "require_current_quote": True,
    },
    "VIX恐慌指数": {
        "source": "cboe_vix",
        "code": "VIX",
        "display_symbol": "VIX",
        "market_group": "美股",
        "require_current_quote": True,
        "show_ma20_deviation": False,
    },
    "日经225": {
        "source": "akshare_global",
        "code": "日经225",
        "yahoo_symbol": "^N225",
        "eastmoney_quote_secid": "100.N225",
        "display_symbol": "N225",
        "market_group": "日本",
        "require_current_quote": True,
    },
    "韩国KOSPI": {
        "source": "akshare_global",
        "code": "韩国KOSPI",
        "yahoo_symbol": "^KS11",
        "eastmoney_quote_secid": "100.KS11",
        "source_correction_start": "2026-07-22",
        "display_symbol": "KOSPI",
        "market_group": "韩国",
        "require_current_quote": True,
    },
    "中证500期货主连": {
        "source": "akshare_futures_main",
        "code": "IC0",
        "display_symbol": "IC0",
        "market_group": "A股",
    },
    "中证1000期货主连": {
        "source": "akshare_futures_main",
        "code": "IM0",
        "display_symbol": "IM0",
        "market_group": "A股",
    },
    "铁矿石主连": {
        "source": "akshare_futures_main",
        "code": "I0",
        "display_symbol": "I0",
        "market_group": "A股",
    },
    "沪金主连": {
        "source": "akshare_futures_main",
        "code": "AU0",
        "display_symbol": "AU0",
        "market_group": "A股",
    },
    "沪银主连": {
        "source": "akshare_futures_main",
        "code": "AG0",
        "display_symbol": "AG0",
        "market_group": "A股",
    },
    "原油主连": {
        "source": "eastmoney_kline",
        "code": "142.scm",
        "display_symbol": "SC0",
        "market_group": "A股",
        "futures_symbol": "SC0",
        "source_correction_start": "2026-07-10",
    },
}


INDEX_LONG_HISTORY_SOURCE = "index_long_history"
INDEX_FINAL_HISTORY_SOURCE = "index_final_history"
INDEX_SOURCE_CORRECTION_SOURCE = "index_source_correction_history"
INDEX_LONG_HISTORY_BARS = 20000
INDEX_REPORT_DISPLAY_DAYS = 120
INDEX_RECENT_GAP_LOOKBACK_SESSIONS = 20
CFFEX_FUTURES_MAIN_PRODUCTS = {
    "IC0": "中证500指数期货",
    "IM0": "中证1000股指期货",
}

__all__ = [
    "YAHOO_CHART_HOSTS", "YAHOO_REQUEST_GATE", "INDEX_CONFIG",
    "INDEX_LONG_HISTORY_SOURCE", "INDEX_FINAL_HISTORY_SOURCE",
    "INDEX_SOURCE_CORRECTION_SOURCE", "INDEX_LONG_HISTORY_BARS",
    "INDEX_REPORT_DISPLAY_DAYS", "INDEX_RECENT_GAP_LOOKBACK_SESSIONS",
    "CFFEX_FUTURES_MAIN_PRODUCTS",
]
