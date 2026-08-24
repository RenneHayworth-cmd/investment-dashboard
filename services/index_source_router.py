from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from services.index_config import (
    CFFEX_FUTURES_MAIN_PRODUCTS, INDEX_CONFIG, INDEX_REPORT_DISPLAY_DAYS,
    YAHOO_CHART_HOSTS, YAHOO_REQUEST_GATE,
)
from services.index_frames import (
    build_export_df, extract_raw_from_export_df, filter_market_trading_dates,
    is_sparse_daily_history, merge_newer_index_rows, merge_raw_index_data,
    normalize_akshare_index_df,
)
from services.market_calendar import (
    expected_latest_trade_date, get_market_window, is_market_holiday,
    is_market_trading_day, latest_completed_trade_date, latest_settled_trade_date,
)

from services.index_sources_akshare import (
    get_index_data_from_akshare_cn, get_index_data_from_akshare_cni,
    get_index_data_from_akshare_csindex, get_index_data_from_akshare_futures_main,
    get_index_data_from_akshare_global, get_index_data_from_akshare_hk,
    get_index_data_from_akshare_us, get_index_data_from_cboe_vix,
)
from services.index_sources_eastmoney import get_index_data_from_eastmoney_kline
from services.index_sources_yahoo import get_index_data_from_yahoo

def fetch_index_from_source(index_name: str, index_config: dict, days: int = 30) -> pd.DataFrame | None:
    source = index_config.get("source")
    code = index_config.get("code")

    if source == "akshare_cn":
        return get_index_data_from_akshare_cn(
            code,
            index_config.get("market", "sh"),
            index_name,
            days=days,
            eastmoney_quote_secid=index_config.get("eastmoney_quote_secid"),
        )
    if source == "akshare_cni":
        return get_index_data_from_akshare_cni(code, index_name, days=days)
    if source == "akshare_csindex":
        return get_index_data_from_akshare_csindex(code, index_name, days=days)
    if source == "akshare_us":
        return get_index_data_from_akshare_us(
            code,
            index_name,
            days=days,
            yahoo_symbol=index_config.get("yahoo_symbol"),
        )
    if source == "akshare_hk":
        return get_index_data_from_akshare_hk(
            code,
            index_name,
            days=days,
            eastmoney_quote_secid=index_config.get("eastmoney_quote_secid"),
        )
    if source == "akshare_global":
        return get_index_data_from_akshare_global(
            code,
            index_name,
            days=days,
            yahoo_symbol=index_config.get("yahoo_symbol"),
            eastmoney_quote_secid=index_config.get("eastmoney_quote_secid"),
            market_name=str(index_config.get("market_group") or ""),
        )
    if source == "akshare_futures_main":
        return get_index_data_from_akshare_futures_main(code, index_name, days=days)
    if source == "eastmoney_kline":
        return get_index_data_from_eastmoney_kline(
            code,
            index_name,
            days=days,
            fqt=str(index_config.get("fqt", "0")),
            akshare_board_symbol=index_config.get("akshare_board_symbol"),
            akshare_hk_em_symbol=index_config.get("akshare_hk_em_symbol"),
        )
    if source == "yahoo":
        return get_index_data_from_yahoo(code, index_name, days=days)
    if source == "cboe_vix":
        return get_index_data_from_cboe_vix(index_name, days=days)
    raise ValueError(f"未知数据源：{source}")

__all__ = ['fetch_index_from_source']
