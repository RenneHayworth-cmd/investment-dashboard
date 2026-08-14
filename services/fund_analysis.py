from __future__ import annotations

from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
import logging
import time

import numpy as np
import pandas as pd
import requests


logger = logging.getLogger(__name__)


FUND_ADJUST_FORWARD_ADDITIVE = "forward_additive"
FUND_ADJUST_FORWARD_RATIO = "forward"
FUND_ADJUST_BACKWARD_ADDITIVE = "backward_additive"
FUND_ADJUST_BACKWARD_RATIO = "backward"
FUND_ADJUST_NONE = "none"
FUND_ADJUSTMENT_VALUES = frozenset(
    {
        FUND_ADJUST_FORWARD_ADDITIVE,
        FUND_ADJUST_FORWARD_RATIO,
        FUND_ADJUST_BACKWARD_ADDITIVE,
        FUND_ADJUST_BACKWARD_RATIO,
        FUND_ADJUST_NONE,
    }
)
FUND_ADJUSTMENT_OPTIONS = {
    "前复权（差值）": FUND_ADJUST_FORWARD_ADDITIVE,
    "前复权（比例）": FUND_ADJUST_FORWARD_RATIO,
    "后复权（差值）": FUND_ADJUST_BACKWARD_ADDITIVE,
    "后复权（比例）": FUND_ADJUST_BACKWARD_RATIO,
    "不复权": FUND_ADJUST_NONE,
}
FUND_ADDITIVE_ADJUSTMENT_OPTIONS = {
    "前复权（差值）": FUND_ADJUST_FORWARD_ADDITIVE,
    "后复权（差值）": FUND_ADJUST_BACKWARD_ADDITIVE,
    "不复权": FUND_ADJUST_NONE,
}
FUND_CACHE_SCHEMA_VERSION = 2


DATE_KEYWORDS = ("日期", "date", "trade_date", "净值日期")
PRICE_KEYWORDS = (
    "累计净值",
    "复权净值",
    "单位净值",
    "净值",
    "nav",
    "net",
    "close",
    "收盘",
    "price",
)


@dataclass
class FundAnalysisResult:
    fund_name: str
    dataframe: pd.DataFrame
    summary: dict[str, str | float | int]
    drawdown_periods: pd.DataFrame = field(default_factory=pd.DataFrame)
    yearly_drawdowns: pd.DataFrame = field(default_factory=pd.DataFrame)


EASTMONEY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


def read_uploaded_table(raw: bytes, filename: str) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(BytesIO(raw))

    errors = []
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            return pd.read_csv(BytesIO(raw), encoding=encoding)
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
    raise ValueError("无法识别 CSV 编码；已尝试 utf-8-sig、utf-8、gbk、gb18030。")


def fetch_eastmoney_fund_nav(
    fund_code: str,
    full_history: bool = True,
    page_size: int = 20,
    max_workers: int = 8,
) -> pd.DataFrame:
    fund_code = fund_code.strip()
    if not fund_code:
        raise ValueError("基金代码不能为空。")
    if not fund_code.isdigit():
        raise ValueError("东方财富基金净值接口需要 6 位数字基金代码，例如 512890。")

    fund_name = fetch_eastmoney_fund_name(fund_code)
    total_count = _fetch_eastmoney_total_count(fund_code)
    if total_count <= 0:
        raise ValueError(f"没有获取到基金 {fund_code} 的净值记录。")

    if not full_history:
        page_df = _fetch_eastmoney_fund_nav_page(fund_code, 1, page_size)
        return _finalize_eastmoney_nav(page_df, fund_code, fund_name=fund_name)

    total_pages = (total_count + page_size - 1) // page_size
    all_pages: list[tuple[int, pd.DataFrame]] = []
    workers = max(1, min(max_workers, total_pages))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_fetch_eastmoney_fund_nav_page, fund_code, page, page_size): page
            for page in range(1, total_pages + 1)
        }
        for future in as_completed(futures):
            page = futures[future]
            page_df = future.result()
            if not page_df.empty:
                all_pages.append((page, page_df))

    if not all_pages:
        raise ValueError(f"没有获取到基金 {fund_code} 的净值记录。")

    all_pages.sort(key=lambda item: item[0])
    merged = pd.concat([page_df for _, page_df in all_pages], ignore_index=True)
    return _finalize_eastmoney_nav(merged, fund_code, fund_name=fund_name)


def fetch_eastmoney_fund_name(fund_code: str) -> str:
    url = "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
    try:
        response = requests.get(
            url,
            params={"m": "1", "key": fund_code},
            headers=EASTMONEY_HEADERS,
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("Datas") or []
        for item in items:
            if str(item.get("CODE", "")).strip() == fund_code:
                name = str(item.get("NAME", "")).strip()
                if name:
                    return name
    except Exception as exc:
        logger.info("东方财富基金 %s 名称获取失败，沿用基金代码：%s", fund_code, exc)
    return fund_code


def _fetch_eastmoney_total_count(fund_code: str) -> int:
    payload = _request_eastmoney_page(fund_code, page_index=1, page_size=1)
    return int(payload.get("TotalCount") or 0)


def _fetch_eastmoney_fund_nav_page(fund_code: str, page_index: int, page_size: int) -> pd.DataFrame:
    time.sleep(0.02)
    payload = _request_eastmoney_page(fund_code, page_index=page_index, page_size=page_size)
    data = payload.get("Data")
    total_count = int(payload.get("TotalCount") or 0)
    if data is None and total_count == 0:
        nav_list = []
    elif not isinstance(data, dict):
        raise ValueError("东方财富接口响应格式异常：Data 字段缺失或不是对象。")
    elif "LSJZList" not in data:
        if total_count == 0:
            nav_list = []
        else:
            raise ValueError("东方财富接口响应格式异常：Data.LSJZList 字段缺失。")
    else:
        nav_list = data["LSJZList"]
        if not isinstance(nav_list, list):
            raise ValueError("东方财富接口响应格式异常：Data.LSJZList 不是列表。")
    rows = []
    for item in nav_list:
        date = item.get("FSRQ")
        accum_nav = item.get("LJJZ")
        unit_nav = item.get("DWJZ")
        daily_growth = item.get("JZZZL")
        if not date:
            continue
        price = accum_nav or unit_nav
        if price in (None, ""):
            continue
        rows.append(
            {
                "日期": date,
                "累计净值": price,
                "单位净值": unit_nav,
                "日增长率(%)": daily_growth,
                "symbol": fund_code,
            }
        )
    return pd.DataFrame(rows)


def _request_eastmoney_page(fund_code: str, page_index: int, page_size: int) -> dict:
    url = "https://api.fund.eastmoney.com/f10/lsjz"
    headers = {
        **EASTMONEY_HEADERS,
        "Referer": f"https://fundf10.eastmoney.com/jjjz_{fund_code}.html",
    }
    response = requests.get(
        url,
        params={"fundCode": fund_code, "pageIndex": page_index, "pageSize": page_size},
        headers=headers,
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("东方财富接口响应格式异常：JSON 根节点不是对象。")
    if payload.get("ErrCode") not in (0, None):
        raise ValueError(payload.get("ErrMsg") or "东方财富接口返回错误。")
    return payload


def _finalize_eastmoney_nav(df: pd.DataFrame, fund_code: str, fund_name: str | None = None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    result = df.copy()
    result["日期"] = pd.to_datetime(result["日期"], errors="coerce")
    for column in ("累计净值", "单位净值", "日增长率(%)"):
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.dropna(subset=["日期", "累计净值"])
    result = result.sort_values("日期").drop_duplicates("日期").reset_index(drop=True)
    result["symbol"] = fund_code
    result["基金名称"] = fund_name or fund_code
    return result


def infer_tickflow_symbol(code: str) -> str:
    code = code.strip().upper()
    if "." in code:
        return code
    if not code.isdigit() or len(code) != 6:
        raise ValueError("场内基金代码请输入 6 位代码，或完整 TickFlow 代码，例如 512890.SH。")
    if code.startswith(("5", "6")):
        return f"{code}.SH"
    if code.startswith(("0", "1", "2", "3")):
        return f"{code}.SZ"
    if code.startswith(("8", "9")):
        return f"{code}.BJ"
    raise ValueError(f"无法推断交易所后缀：{code}，请改为输入完整代码，例如 {code}.SH。")


def normalize_fund_adjustment(adjust: str | None) -> str:
    """Return one explicit adjustment mode; legacy None means unadjusted."""
    normalized = FUND_ADJUST_NONE if adjust is None else str(adjust).strip().lower()
    if normalized not in FUND_ADJUSTMENT_VALUES:
        supported = "、".join(sorted(FUND_ADJUSTMENT_VALUES))
        raise ValueError(f"不支持的复权方式：{adjust}；可选值为 {supported}。")
    return normalized


def build_fund_cache_symbol(prefix: str, symbol: str, adjust: str | None) -> str:
    mode = normalize_fund_adjustment(adjust)
    return f"{prefix}_v{FUND_CACHE_SCHEMA_VERSION}_{symbol}_{mode}"


def stamp_fund_history_metadata(
    df: pd.DataFrame,
    adjust: str | None,
) -> pd.DataFrame:
    result = df.copy()
    result["_adjust_mode"] = normalize_fund_adjustment(adjust)
    result["_cache_schema_version"] = FUND_CACHE_SCHEMA_VERSION
    return result


def fetch_tickflow_fund_close(
    symbol: str,
    api_key: str = "",
    count: int = 5000,
    adjust: str | None = FUND_ADJUST_FORWARD_ADDITIVE,
) -> pd.DataFrame:
    from tickflow import TickFlow

    adjustment = normalize_fund_adjustment(adjust)
    client = TickFlow(api_key=api_key) if api_key else TickFlow.free()
    fund_name = fetch_tickflow_instrument_name(symbol, api_key=api_key)
    kwargs = {
        "period": "1d",
        "count": count,
        "as_dataframe": True,
        "adjust": adjustment,
    }
    df = client.klines.get(symbol, **kwargs)
    if df is None or df.empty:
        raise ValueError(f"TickFlow 未返回 {symbol} 的日线数据。")

    normalized = df.copy()
    normalized.columns = [str(col).strip() for col in normalized.columns]
    if "trade_date" not in normalized.columns or "close" not in normalized.columns:
        raise ValueError(f"TickFlow 返回列无法识别：{list(normalized.columns)}")

    columns = ["trade_date", "close"]
    if "open" in normalized.columns:
        columns.insert(1, "open")
    result = normalized[columns].copy()
    result.columns = ["日期", "开盘价", "收盘价"] if "open" in normalized.columns else ["日期", "收盘价"]
    result["日期"] = pd.to_datetime(result["日期"], errors="coerce")
    if "开盘价" in result.columns:
        result["开盘价"] = pd.to_numeric(result["开盘价"], errors="coerce")
    result["收盘价"] = pd.to_numeric(result["收盘价"], errors="coerce")
    result = result.dropna(subset=["日期", "收盘价"])
    result = result.sort_values("日期").drop_duplicates("日期").reset_index(drop=True)
    result["symbol"] = symbol
    result["name"] = fund_name or symbol
    return stamp_fund_history_metadata(result, adjustment)


def fetch_tickflow_instrument_name(symbol: str, api_key: str = "") -> str:
    try:
        from tickflow import TickFlow

        client = TickFlow(api_key=api_key) if api_key else TickFlow.free()
        instruments = client.instruments.batch(symbols=[symbol])
        if instruments:
            name = str(instruments[0].get("name", "")).strip()
            if name:
                return name
    except Exception:
        pass
    return symbol


def _find_column(columns: list[str], keywords: tuple[str, ...]) -> str | None:
    for keyword in keywords:
        keyword_lower = keyword.lower()
        for column in columns:
            if keyword_lower in str(column).strip().lower():
                return column
    return None


def normalize_nav_dataframe(df: pd.DataFrame, fallback_name: str = "基金") -> tuple[str, pd.DataFrame]:
    if df is None or df.empty:
        raise ValueError("文件中没有可分析的数据。")

    normalized = df.copy()
    normalized.columns = [str(col).strip() for col in normalized.columns]
    columns = list(normalized.columns)

    date_col = _find_column(columns, DATE_KEYWORDS)
    price_col = _find_column(columns, PRICE_KEYWORDS)
    if not date_col or not price_col:
        raise ValueError(f"无法识别日期列或净值/价格列。当前列名：{columns}")

    name = fallback_name
    for name_col in ("基金名称", "name", "名称", "指数名称", "symbol"):
        if name_col in normalized.columns and normalized[name_col].notna().any():
            name = str(normalized[name_col].dropna().iloc[0])
            break

    result = normalized[[date_col, price_col]].copy()
    result.columns = ["date", "price"]
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    result["price"] = pd.to_numeric(result["price"], errors="coerce")
    result = result.dropna(subset=["date", "price"])
    result = result.sort_values("date").drop_duplicates("date").reset_index(drop=True)

    if result.empty:
        raise ValueError("日期和净值列解析后没有有效数据。")

    return name, result


def should_use_log_price_axis(
    df: pd.DataFrame,
    price_columns: list[str] | tuple[str, ...] = ("price",),
    date_column: str = "date",
    min_calendar_days: int = 365 * 3,
    min_rows: int = 252 * 3,
) -> bool:
    if df is None or df.empty or date_column not in df.columns:
        return False

    dates = pd.to_datetime(df[date_column], errors="coerce").dropna()
    if dates.empty:
        return False

    is_long_history = (dates.max() - dates.min()).days >= min_calendar_days or len(df) >= min_rows
    if not is_long_history:
        return False

    for column in price_columns:
        if column not in df.columns:
            continue
        values = pd.to_numeric(df[column], errors="coerce").dropna()
        if not values.empty and (values <= 0).any():
            return False

    return True


def resolve_price_axis_type(
    df: pd.DataFrame,
    axis_mode: str,
    price_columns: list[str] | tuple[str, ...] = ("price",),
    date_column: str = "date",
) -> str:
    if axis_mode == "普通坐标":
        return "linear"

    has_non_positive = False
    for column in price_columns:
        if column not in df.columns:
            continue
        values = pd.to_numeric(df[column], errors="coerce").dropna()
        if not values.empty and (values <= 0).any():
            has_non_positive = True
            break

    if axis_mode == "对数坐标":
        return "linear" if has_non_positive else "log"

    return (
        "log"
        if should_use_log_price_axis(
            df,
            price_columns=price_columns,
            date_column=date_column,
        )
        else "linear"
    )


def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(~((loss == 0) & (gain > 0)), 100)
    rsi = rsi.where(~((loss == 0) & (gain == 0)), 50)
    return rsi


def analyze_fund_nav(
    df: pd.DataFrame,
    fund_name: str,
    ma_periods: list[int] | tuple[int, ...] = (20, 60, 120, 250),
    rsi_period: int = 14,
    base_date: str = "2024-09-30",
) -> FundAnalysisResult:
    result = df.copy()
    prices = result["price"]
    returns = prices.pct_change()

    result["daily_return_pct"] = returns * 100
    result["return_20d_pct"] = (prices / prices.shift(20) - 1) * 100
    result["return_60d_pct"] = (prices / prices.shift(60) - 1) * 100
    result["volatility_20d_pct"] = returns.rolling(window=20).std() * 100
    result["momentum_volatility_20d"] = np.where(
        result["volatility_20d_pct"] != 0,
        (result["return_20d_pct"] / 100) / (result["volatility_20d_pct"] / 100),
        np.nan,
    )
    result[f"rsi_{rsi_period}"] = calculate_rsi(prices, rsi_period)
    result["price_percentile"] = prices.expanding().rank(pct=True) * 100

    for period in ma_periods:
        ma_col = f"ma_{period}"
        result[ma_col] = prices.rolling(window=period).mean()
        result[f"ma_{period}_deviation_pct"] = (prices / result[ma_col] - 1) * 100

    result["year"] = result["date"].dt.year
    ytd_start = result.groupby("year")["price"].transform("first")
    result["ytd_return_pct"] = (prices / ytd_start - 1) * 100
    result = result.drop(columns=["year"])

    base_ts = pd.Timestamp(base_date)
    base_rows = result[result["date"] >= base_ts]
    if base_rows.empty:
        result["base_date_return_pct"] = np.nan
    else:
        base_price = base_rows.iloc[0]["price"]
        result["base_date_return_pct"] = np.where(
            result["date"] >= base_rows.iloc[0]["date"],
            (prices / base_price - 1) * 100,
            np.nan,
        )

    running_max = prices.cummax()
    result["drawdown_pct"] = (prices / running_max - 1) * 100
    result["running_peak"] = running_max
    drawdown_periods = extract_drawdown_periods(result)
    yearly_drawdowns = calculate_yearly_drawdowns(result)
    max_drawdown_info = calculate_max_drawdown_info(result)

    trading_days = len(result)
    if trading_days >= 252 * 3:
        rolling_window = 252 * 3
        rolling_label = "三年滚动年化收益率(%)"
    elif trading_days >= 252:
        rolling_window = 252
        rolling_label = "一年滚动年化收益率(%)"
    else:
        rolling_window = 0
        rolling_label = "滚动年化收益率(%)"

    result["rolling_annual_return_pct"] = np.nan
    if rolling_window:
        result["rolling_annual_return_pct"] = (
            (prices / prices.shift(rolling_window)) ** (252 / rolling_window) - 1
        ) * 100

    latest = result.iloc[-1]
    valid_returns = returns.dropna()
    annual_volatility = valid_returns.std() * np.sqrt(252) * 100 if not valid_returns.empty else np.nan

    summary: dict[str, str | float | int] = {
        "基金名称": fund_name,
        "起始日期": result["date"].min().strftime("%Y-%m-%d"),
        "最新日期": latest["date"].strftime("%Y-%m-%d"),
        "数据行数": len(result),
        "最新价格": round(float(latest["price"]), 4),
        "20日涨幅(%)": _round_or_nan(latest["return_20d_pct"]),
        "60日涨幅(%)": _round_or_nan(latest["return_60d_pct"]),
        "YTD涨幅(%)": _round_or_nan(latest["ytd_return_pct"]),
        f"{base_date}以来涨幅(%)": _round_or_nan(latest["base_date_return_pct"]),
        f"RSI({rsi_period})": _round_or_nan(latest[f"rsi_{rsi_period}"]),
        "价格百分位": _round_or_nan(latest["price_percentile"]),
        "最大回撤(%)": _round_or_nan(result["drawdown_pct"].min(), digits=2),
        "最大回撤峰值日": max_drawdown_info.get("峰值日期", ""),
        "最大回撤谷底日": max_drawdown_info.get("谷底日期", ""),
        "最大回撤修复日": max_drawdown_info.get("修复完成日期", ""),
        "最大回撤下跌天数": max_drawdown_info.get("回撤天数", 0),
        "最大回撤修复天数": max_drawdown_info.get("修复天数", 0),
        "最大回撤是否已修复": max_drawdown_info.get("是否已修复", False),
        "年化波动率(%)": _round_or_nan(annual_volatility),
        "滚动年化类型": rolling_label,
        rolling_label: _round_or_nan(latest["rolling_annual_return_pct"]),
    }

    for period in ma_periods:
        deviation_col = f"ma_{period}_deviation_pct"
        if deviation_col in result.columns:
            summary[f"MA{period}偏离(%)"] = _round_or_nan(latest[deviation_col])

    rounded = result.copy()
    for column in rounded.select_dtypes(include=[np.number]).columns:
        rounded[column] = rounded[column].round(4)
    for column in ("drawdown_pct",):
        if column in rounded.columns:
            rounded[column] = rounded[column].round(2)

    return FundAnalysisResult(
        fund_name=fund_name,
        dataframe=rounded,
        summary=summary,
        drawdown_periods=drawdown_periods,
        yearly_drawdowns=yearly_drawdowns,
    )


def calculate_max_drawdown_info(df: pd.DataFrame) -> dict[str, object]:
    if df is None or len(df) < 2:
        return {}

    data = df.sort_values("date").reset_index(drop=True)
    prices = data["price"].to_numpy()
    dates = pd.to_datetime(data["date"]).to_numpy()
    running_max = np.maximum.accumulate(prices)
    drawdown = (prices / running_max - 1) * 100
    trough_idx = int(np.argmin(drawdown))
    peak_idx = int(np.argmax(running_max[: trough_idx + 1]))
    peak_value = prices[peak_idx]

    recovery_idx = None
    for idx in range(trough_idx + 1, len(prices)):
        if prices[idx] >= peak_value:
            recovery_idx = idx
            break

    peak_date = pd.Timestamp(dates[peak_idx])
    trough_date = pd.Timestamp(dates[trough_idx])
    if recovery_idx is not None:
        recovery_date = pd.Timestamp(dates[recovery_idx])
        recovery_days = int((recovery_date - trough_date).days)
        is_recovered = True
    else:
        recovery_date = pd.Timestamp(dates[-1])
        recovery_days = int((recovery_date - trough_date).days)
        is_recovered = False

    return {
        "峰值日期": peak_date.strftime("%Y-%m-%d"),
        "谷底日期": trough_date.strftime("%Y-%m-%d"),
        "修复完成日期": recovery_date.strftime("%Y-%m-%d"),
        "回撤深度(%)": round(float(drawdown[trough_idx]), 2),
        "回撤天数": int((trough_date - peak_date).days),
        "修复天数": recovery_days,
        "是否已修复": is_recovered,
    }


def calculate_current_drawdown_info(df: pd.DataFrame) -> dict[str, object]:
    if df is None or len(df) < 1:
        return {}

    data = df.sort_values("date").reset_index(drop=True).copy()
    prices = pd.to_numeric(data["price"], errors="coerce")
    dates = pd.to_datetime(data["date"], errors="coerce")
    valid = pd.DataFrame({"date": dates, "price": prices}).dropna(subset=["date", "price"])
    if valid.empty:
        return {}

    valid = valid.reset_index(drop=True)
    running_peak = valid["price"].cummax()
    drawdown_pct = (valid["price"] / running_peak - 1) * 100
    latest_idx = len(valid) - 1
    latest_date = valid.loc[latest_idx, "date"]
    latest_drawdown = float(drawdown_pct.iloc[latest_idx])

    peak_value = float(running_peak.iloc[latest_idx])
    peak_matches = valid.index[valid["price"].round(10) >= round(peak_value, 10)].tolist()
    peak_idx = peak_matches[-1] if peak_matches else int(running_peak.iloc[: latest_idx + 1].idxmax())
    peak_date = valid.loc[peak_idx, "date"]

    if latest_drawdown >= -1e-10:
        return {
            "当前回撤(%)": 0.0,
            "当前回撤峰值日": latest_date.strftime("%Y-%m-%d"),
            "当前谷底日": latest_date.strftime("%Y-%m-%d"),
            "当前回撤时间": 0,
            "当前回撤是否已修复": True,
            "当前修复状态": "无当前回撤",
        }

    current_period = drawdown_pct.iloc[peak_idx : latest_idx + 1]
    trough_idx = int(current_period.idxmin())
    trough_date = valid.loc[trough_idx, "date"]
    drawdown_days = int((latest_date - peak_date).days)

    return {
        "当前回撤(%)": round(latest_drawdown, 2),
        "当前回撤峰值日": peak_date.strftime("%Y-%m-%d"),
        "当前谷底日": trough_date.strftime("%Y-%m-%d"),
        "当前回撤时间": drawdown_days,
        "当前回撤是否已修复": False,
        "当前修复状态": f"未修复，截至 {latest_date.strftime('%Y-%m-%d')}",
    }


def extract_drawdown_periods(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) < 2:
        return pd.DataFrame()

    data = df.sort_values("date").reset_index(drop=True)
    prices = data["price"].to_numpy()
    dates = pd.to_datetime(data["date"]).to_numpy()
    running_max = np.maximum.accumulate(prices)
    drawdown = (prices / running_max - 1) * 100

    periods = []
    idx = 0
    while idx < len(drawdown):
        if drawdown[idx] < 0 and (idx == 0 or drawdown[idx - 1] >= 0):
            start_idx = idx
            trough_idx = idx
            while idx < len(drawdown) and drawdown[idx] < 0:
                if drawdown[idx] < drawdown[trough_idx]:
                    trough_idx = idx
                idx += 1

            recovery_idx = None
            peak_value = running_max[start_idx]
            for recovery_candidate in range(trough_idx + 1, len(prices)):
                if prices[recovery_candidate] >= peak_value:
                    recovery_idx = recovery_candidate
                    break

            start_date = pd.Timestamp(dates[start_idx])
            trough_date = pd.Timestamp(dates[trough_idx])
            end_idx = recovery_idx if recovery_idx is not None else len(prices) - 1
            recovery_date = pd.Timestamp(dates[end_idx])
            periods.append(
                {
                    "回撤开始日期": start_date.strftime("%Y-%m-%d"),
                    "谷底日期": trough_date.strftime("%Y-%m-%d"),
                    "修复完成日期": recovery_date.strftime("%Y-%m-%d"),
                    "回撤深度(%)": round(float(drawdown[trough_idx]), 2),
                    "回撤天数": int((trough_date - start_date).days),
                    "修复天数": int((recovery_date - trough_date).days),
                    "是否已修复": recovery_idx is not None,
                }
            )
        else:
            idx += 1

    if not periods:
        return pd.DataFrame()

    periods_df = pd.DataFrame(periods)
    max_drawdown_abs = abs(float(periods_df["回撤深度(%)"].min()))
    if max_drawdown_abs > 0:
        min_depth_abs = max_drawdown_abs / 4
        periods_df = periods_df[periods_df["回撤深度(%)"].abs() >= min_depth_abs]
    return periods_df.sort_values("回撤开始日期").reset_index(drop=True)


def calculate_yearly_drawdowns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) < 2:
        return pd.DataFrame()

    rows = []
    data = df.sort_values("date").reset_index(drop=True).copy()
    data["year"] = data["date"].dt.year
    for year, group in data.groupby("year"):
        if len(group) < 2:
            continue
        group = group.reset_index(drop=True)
        prices = group["price"].to_numpy()
        dates = pd.to_datetime(group["date"]).to_numpy()
        running_max = np.maximum.accumulate(prices)
        drawdown = (prices / running_max - 1) * 100
        trough_idx = int(np.argmin(drawdown))
        peak_idx = int(np.argmax(running_max[: trough_idx + 1]))
        peak_value = prices[peak_idx]

        recovery_idx = None
        for idx in range(trough_idx + 1, len(prices)):
            if prices[idx] >= peak_value:
                recovery_idx = idx
                break

        trough_date = pd.Timestamp(dates[trough_idx])
        if recovery_idx is not None:
            recovery_date = pd.Timestamp(dates[recovery_idx])
            recovery_days = int((recovery_date - trough_date).days)
            is_recovered = True
        else:
            recovery_date = pd.Timestamp(dates[-1])
            recovery_days = None
            is_recovered = False

        rows.append(
            {
                "年份": int(year),
                "年度最大回撤(%)": round(float(drawdown[trough_idx]), 2),
                "最大回撤发生日期": trough_date.strftime("%Y-%m-%d"),
                "修复完成日期": recovery_date.strftime("%Y-%m-%d"),
                "修复时间(天)": recovery_days,
                "是否已修复": is_recovered,
            }
        )

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("年份", ascending=False).reset_index(drop=True)


def _round_or_nan(value: object, digits: int = 4) -> float:
    if pd.isna(value):
        return float("nan")
    return round(float(value), digits)
