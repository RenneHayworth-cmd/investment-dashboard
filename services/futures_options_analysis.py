from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import pandas as pd


DATA_TYPE_AUTO = "自动"
DATA_TYPE_FUTURES = "期货"
DATA_TYPE_OPTIONS = "期权"
FUTURES_OPTION_DATA_VERSION = "futures_option_v2"

FINANCIAL_FUTURES_PREFIXES = ("IF", "IC", "IH", "IM", "T", "TF", "TS", "TL")
DCE_PREFIXES = ("i", "j", "jm", "l", "m", "p", "pp", "v", "y", "a", "b", "c", "cs", "eg", "eb", "pg", "lh")
SHF_PREFIXES = ("au", "ag", "cu", "al", "zn", "pb", "ni", "sn", "rb", "hc", "ss", "wr", "fu", "bu", "ru", "sp", "ao", "br")
INE_PREFIXES = ("sc", "lu", "bc", "nr", "ec")
GFE_PREFIXES = ("si", "lc", "ps")
CFFEX_OPTION_PREFIXES = ("io", "ho", "mo")
MAIN_CONTINUOUS_MARKERS = ("主连", "MAIN", "main", "M0", "m0", "连续")


@dataclass
class FuturesOptionResult:
    symbol: str
    source: str
    data_kind: str
    dataframe: pd.DataFrame
    summary: dict[str, object]
    is_chain: bool = False


def normalize_symbol(symbol: str) -> str:
    cleaned = symbol.strip()
    if not cleaned:
        raise ValueError("合约代码不能为空。")
    if "." in cleaned:
        code, suffix = cleaned.rsplit(".", 1)
        suffix = suffix.upper()
        if suffix == "CFX":
            return f"{code.upper()}.{suffix}"
        return f"{code.lower()}.{suffix}"

    upper = cleaned.upper()
    lower = cleaned.lower()
    product_match = re.match(r"([A-Za-z]+)", cleaned)
    product = product_match.group(1).lower() if product_match else ""
    product_upper = product.upper()

    if product_upper in FINANCIAL_FUTURES_PREFIXES:
        return f"{upper}.CFX"
    if product in DCE_PREFIXES:
        return f"{lower}.DCE"
    if product in SHF_PREFIXES:
        return f"{lower}.SHF"
    if product in INE_PREFIXES:
        return f"{lower}.INE"
    if product in GFE_PREFIXES:
        return f"{lower}.GFE"
    return cleaned


def normalize_main_continuous_symbol(symbol: str) -> str | None:
    cleaned = symbol.strip()
    if not cleaned:
        return None

    explicit_match = re.match(r"^([A-Za-z]+)0$", cleaned)
    if explicit_match:
        return f"{explicit_match.group(1).upper()}0"

    for marker in MAIN_CONTINUOUS_MARKERS:
        if marker in cleaned:
            product = cleaned.replace(marker, "")
            product = product.replace(".", "").replace("_", "").replace("-", "").strip()
            if product and re.fullmatch(r"[A-Za-z]+", product):
                return f"{product.upper()}0"
    return None


def is_main_continuous_symbol(symbol: str) -> bool:
    return normalize_main_continuous_symbol(symbol) is not None


def _main_contract_candidates(main_symbol: str, months: int = 12) -> tuple[list[str], str]:
    product = main_symbol[:-1]
    if not product:
        return [], "CF"

    market = "CFFEX" if product.upper() in FINANCIAL_FUTURES_PREFIXES else "CF"
    now = datetime.now()
    candidates = []
    for offset in range(months):
        month_value = now.month + offset
        year = now.year + (month_value - 1) // 12
        month = (month_value - 1) % 12 + 1
        candidates.append(f"{product.upper()}{str(year)[-2:]}{month:02d}")
    return candidates, market


def infer_current_main_contract(main_symbol: str) -> str | None:
    candidates, market = _main_contract_candidates(main_symbol)
    if not candidates:
        return None

    import akshare as ak

    df = ak.futures_zh_spot(symbol=",".join(candidates), market=market, adjust="0")
    if df is None or df.empty or "hold" not in df.columns:
        return None

    result = df.copy()
    result["hold"] = pd.to_numeric(result["hold"], errors="coerce")
    result = result.dropna(subset=["hold"])
    if result.empty:
        return None

    best_symbol = str(result.sort_values("hold", ascending=False).iloc[0].get("symbol", ""))
    matches = re.findall(r"(\d{4})", best_symbol)
    if not matches:
        return None
    product = main_symbol[:-1].upper()
    return f"{product}{matches[-1]}"


def option_base_symbol(symbol: str) -> str:
    return symbol.split(".", 1)[0]


def akshare_symbol(symbol: str) -> str:
    return symbol.split(".", 1)[0]


def normalize_option_symbol(symbol: str) -> str:
    base = option_base_symbol(symbol.strip())
    match = re.match(r"^([A-Za-z]+)(\d{4})([CPcp])?(\d+)?$", base)
    if not match:
        return base

    product, month, option_type, strike = match.groups()
    product = product.lower()
    if option_type and strike:
        return f"{product}{month}{option_type.upper()}{strike}"
    return f"{product}{month}"


def _today_china() -> pd.Timestamp:
    return pd.Timestamp.now(tz="Asia/Shanghai").normalize().tz_localize(None)


def is_option_symbol(symbol: str) -> bool:
    base = normalize_option_symbol(symbol)
    match = re.match(r"^([a-z]+)\d{4}([CP]\d+)?$", base)
    if not match:
        return False
    product = match.group(1)
    has_strike = match.group(2) is not None
    return product in CFFEX_OPTION_PREFIXES or (product == "i" and has_strike)


def should_fetch_options(symbol: str, data_type: str) -> bool:
    if data_type == DATA_TYPE_OPTIONS:
        return True
    if data_type == DATA_TYPE_FUTURES:
        return False
    return is_option_symbol(symbol)


def fetch_from_tickflow(symbol: str, period: str, count: int, api_key: str, use_free: bool) -> pd.DataFrame:
    from tickflow import TickFlow

    client = TickFlow.free() if use_free else TickFlow(api_key=api_key)
    return client.klines.get(symbol, period=period, count=count, as_dataframe=True)


def fetch_from_akshare(symbol: str, period: str, count: int) -> pd.DataFrame:
    if period != "1d":
        raise RuntimeError("AkShare 回退目前只支持日线周期 1d。")

    import akshare as ak

    df = ak.futures_zh_daily_sina(symbol=akshare_symbol(symbol))
    if df is None or df.empty:
        raise RuntimeError("TickFlow 和 AkShare 都没有获取到数据，请检查合约代码。")
    return df.tail(count).reset_index(drop=True)


def fetch_option_from_akshare(symbol: str, period: str, count: int) -> tuple[pd.DataFrame, str, bool]:
    import akshare as ak

    option_symbol = normalize_option_symbol(symbol)
    match = re.match(r"^([a-z]+)\d{4}([CP]\d+)?$", option_symbol)
    if not match:
        raise RuntimeError("期权代码格式无法识别。示例：mo2606、mo2606C5800、i2606、i2606C800。")

    product = match.group(1)
    is_contract = match.group(2) is not None

    if product == "mo":
        daily_func = ak.option_cffex_zz1000_daily_sina
        chain_func = ak.option_cffex_zz1000_spot_sina
    elif product == "io":
        daily_func = ak.option_cffex_hs300_daily_sina
        chain_func = ak.option_cffex_hs300_spot_sina
    elif product == "ho":
        daily_func = ak.option_cffex_sz50_daily_sina
        chain_func = ak.option_cffex_sz50_spot_sina
    elif product == "i":
        daily_func = ak.option_commodity_hist_sina
        chain_func = None
    else:
        raise RuntimeError("当前只支持股指期权 io/ho/mo 和铁矿石期权 i。")

    if is_contract:
        if period != "1d":
            raise RuntimeError("期权历史行情目前只支持日线周期 1d。")
        df = daily_func(symbol=option_symbol)
        if df is None or df.empty:
            raise RuntimeError("AkShare 没有获取到期权日线数据，请检查月份、行权价或合约是否存在。")
        df = append_option_spot_row(df, option_symbol)
        return df.tail(count).reset_index(drop=True), "AkShare期权日线/实时快照", False

    if product == "i":
        df = ak.option_commodity_contract_table_sina(symbol="铁矿石期权", contract=option_symbol)
    else:
        df = chain_func(symbol=option_symbol)
    if df is None or df.empty:
        raise RuntimeError("AkShare 没有获取到期权链数据，请检查月份是否存在。")
    return df.reset_index(drop=True), "AkShare期权链", True


def append_option_spot_row(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    option_symbol = normalize_option_symbol(symbol)
    match = re.match(r"^([a-z]+)(\d{4})([CP])(\d+)$", option_symbol)
    if not match:
        return df

    product, month, option_type, strike = match.groups()
    today = _today_china()
    if today.weekday() >= 5:
        return df

    result = df.copy()
    date_col = "date" if "date" in result.columns else "日期" if "日期" in result.columns else None
    if date_col is not None:
        dates = pd.to_datetime(result[date_col], errors="coerce")
        latest_history_date = dates.dropna().max()
        if pd.notna(latest_history_date) and latest_history_date.normalize() >= today:
            return result

    try:
        import akshare as ak

        if product == "i":
            chain_df = ak.option_commodity_contract_table_sina(
                symbol="铁矿石期权",
                contract=f"{product}{month}",
            )
        elif product == "mo":
            chain_df = ak.option_cffex_zz1000_spot_sina(symbol=f"{product}{month}")
        elif product == "io":
            chain_df = ak.option_cffex_hs300_spot_sina(symbol=f"{product}{month}")
        elif product == "ho":
            chain_df = ak.option_cffex_sz50_spot_sina(symbol=f"{product}{month}")
        else:
            return result
    except Exception:
        return result

    if chain_df is None or chain_df.empty:
        return result

    side = "看跌" if option_type == "P" else "看涨"
    contract_col = f"{side}合约-{side}期权合约"
    price_col = f"{side}合约-最新价"
    hold_col = f"{side}合约-持仓量"
    bid_col = f"{side}合约-买价"
    ask_col = f"{side}合约-卖价"
    change_col = f"{side}合约-涨跌"
    required_cols = {contract_col, price_col}
    if not required_cols.issubset(set(chain_df.columns)):
        return result

    matched = chain_df[
        chain_df[contract_col].astype(str).str.lower() == option_symbol.lower()
    ]
    if matched.empty:
        return result

    row = matched.iloc[0]
    latest_price = pd.to_numeric(row.get(price_col), errors="coerce")
    if pd.isna(latest_price) or latest_price <= 0:
        return result

    supplement = {
        "date": today,
        "close": float(latest_price),
    }
    if hold_col in row.index:
        supplement["open_interest"] = pd.to_numeric(row.get(hold_col), errors="coerce")
    if bid_col in row.index:
        supplement["bid_price"] = pd.to_numeric(row.get(bid_col), errors="coerce")
    if ask_col in row.index:
        supplement["ask_price"] = pd.to_numeric(row.get(ask_col), errors="coerce")
    if change_col in row.index:
        supplement["spot_change_pct"] = pd.to_numeric(row.get(change_col), errors="coerce")

    return (
        pd.concat([result, pd.DataFrame([supplement])], ignore_index=True)
        .assign(date=lambda data: pd.to_datetime(data["date"], errors="coerce"))
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )


def normalize_market_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        raise ValueError("返回空数据。")

    result = df.copy()
    result.columns = [str(column).strip() for column in result.columns]
    rename_map = {}
    for column in result.columns:
        text = str(column).strip().lower()
        if text in {"trade_date", "date", "日期", "交易日期"}:
            rename_map[column] = "date"
        elif text in {"open", "开盘", "开盘价"}:
            rename_map[column] = "open"
        elif text in {"high", "最高", "最高价"}:
            rename_map[column] = "high"
        elif text in {"low", "最低", "最低价"}:
            rename_map[column] = "low"
        elif text in {"close", "收盘", "收盘价", "最新价"}:
            rename_map[column] = "close"
        elif text in {"volume", "成交量"}:
            rename_map[column] = "volume"
        elif text in {"amount", "成交额"}:
            rename_map[column] = "amount"
        elif text in {"open_interest", "持仓量"}:
            rename_map[column] = "open_interest"

    result = result.rename(columns=rename_map)
    if "date" not in result.columns or "close" not in result.columns:
        raise ValueError(f"无法识别日期列或收盘价列。当前列名：{list(df.columns)}")

    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "amount", "open_interest"):
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")

    result = result.dropna(subset=["date", "close"]).sort_values("date").drop_duplicates("date")
    if result.empty:
        raise ValueError("日期和收盘价解析后没有有效数据。")
    return result.reset_index(drop=True)


def choose_latest_futures_daily(
    tickflow_df: pd.DataFrame | None,
    akshare_df: pd.DataFrame | None,
) -> tuple[pd.DataFrame | None, str]:
    if tickflow_df is None or tickflow_df.empty:
        return akshare_df, "AkShare"
    if akshare_df is None or akshare_df.empty:
        return tickflow_df, "TickFlow"

    tickflow_normalized = normalize_market_dataframe(tickflow_df)
    akshare_normalized = normalize_market_dataframe(akshare_df)
    tickflow_latest = tickflow_normalized["date"].max()
    akshare_latest = akshare_normalized["date"].max()
    if pd.notna(akshare_latest) and pd.notna(tickflow_latest) and akshare_latest > tickflow_latest:
        return akshare_df, "AkShare（TickFlow数据不完整）"
    return tickflow_df, "TickFlow"


def build_summary(df: pd.DataFrame) -> dict[str, object]:
    close = pd.to_numeric(df["close"], errors="coerce")
    latest = df.iloc[-1]
    returns = close.pct_change()
    start_close = close.dropna().iloc[0]
    latest_close = close.dropna().iloc[-1]
    summary: dict[str, object] = {
        "最新日期": pd.Timestamp(latest["date"]).strftime("%Y-%m-%d"),
        "数据行数": len(df),
        "最新收盘": round(float(latest_close), 4),
        "区间涨跌幅(%)": round(float((latest_close / start_close - 1) * 100), 2) if start_close else float("nan"),
        "20日涨跌幅(%)": round(float((latest_close / close.shift(20).iloc[-1] - 1) * 100), 2)
        if len(close.dropna()) > 20 and close.shift(20).iloc[-1]
        else float("nan"),
        "20日波动率(%)": round(float(returns.rolling(20).std().iloc[-1] * 100), 2)
        if len(returns.dropna()) >= 20
        else float("nan"),
        "价格百分位": round(float(close.expanding().rank(pct=True).iloc[-1] * 100), 2),
    }
    if "volume" in df.columns:
        summary["最新成交量"] = latest.get("volume")
    if "open_interest" in df.columns:
        summary["最新持仓量"] = latest.get("open_interest")
    return summary


def add_indicators(df: pd.DataFrame, ma_periods: list[int] | tuple[int, ...]) -> pd.DataFrame:
    result = df.copy()
    result["daily_return_pct"] = result["close"].pct_change() * 100
    for period in ma_periods:
        result[f"ma_{period}"] = result["close"].rolling(period).mean()
    numeric_cols = result.select_dtypes(include="number").columns
    result[numeric_cols] = result[numeric_cols].round(4)
    return result


def fetch_futures_option_data(
    raw_symbol: str,
    data_type: str,
    period: str,
    count: int,
    api_key: str,
    use_free: bool,
    ma_periods: list[int] | tuple[int, ...],
) -> FuturesOptionResult:
    if should_fetch_options(raw_symbol, data_type):
        symbol = normalize_option_symbol(raw_symbol)
        raw_df, source, is_chain = fetch_option_from_akshare(symbol, period, count)
        if is_chain:
            return FuturesOptionResult(
                symbol=symbol,
                source=source,
                data_kind="期权链",
                dataframe=raw_df,
                summary={"数据行数": len(raw_df)},
                is_chain=True,
            )
        normalized = normalize_market_dataframe(raw_df)
        analyzed = add_indicators(normalized, ma_periods)
        analyzed["_data_version"] = FUTURES_OPTION_DATA_VERSION
        return FuturesOptionResult(
            symbol=symbol,
            source=source,
            data_kind="期权日线",
            dataframe=analyzed,
            summary=build_summary(analyzed),
        )

    main_symbol = normalize_main_continuous_symbol(raw_symbol)
    if main_symbol:
        raw_df = fetch_from_akshare(main_symbol, period, count)
        normalized = normalize_market_dataframe(raw_df)
        current_main = None
        try:
            current_main = infer_current_main_contract(main_symbol)
        except Exception:
            current_main = None
        if current_main:
            normalized["current_main_contract"] = current_main
        analyzed = add_indicators(normalized, ma_periods)
        analyzed["_data_version"] = FUTURES_OPTION_DATA_VERSION
        return FuturesOptionResult(
            symbol=main_symbol,
            source="AkShare主连",
            data_kind="期货主连",
            dataframe=analyzed,
            summary=build_summary(analyzed),
        )

    symbol = normalize_symbol(raw_symbol)
    source = "TickFlow"
    tickflow_error = None
    try:
        tickflow_df = fetch_from_tickflow(symbol, period, count, api_key, use_free)
    except Exception as exc:
        tickflow_df = None
        tickflow_error = exc

    akshare_df = None
    akshare_error = None
    if period == "1d":
        try:
            akshare_df = fetch_from_akshare(symbol, period, count)
        except Exception as exc:
            akshare_error = exc

    try:
        raw_df, source = choose_latest_futures_daily(tickflow_df, akshare_df)
    except Exception:
        raw_df = tickflow_df

    if raw_df is None or raw_df.empty:
        if tickflow_error is not None and akshare_error is not None:
            raise RuntimeError(f"TickFlow 失败：{tickflow_error}；AkShare 失败：{akshare_error}") from akshare_error
        if tickflow_error is not None:
            raise RuntimeError(f"TickFlow 失败：{tickflow_error}") from tickflow_error
        if akshare_error is not None:
            raise RuntimeError(f"AkShare 失败：{akshare_error}") from akshare_error
        raise RuntimeError("TickFlow 和 AkShare 都没有获取到数据，请检查合约代码。")

    normalized = normalize_market_dataframe(raw_df)
    analyzed = add_indicators(normalized, ma_periods)
    analyzed["_data_version"] = FUTURES_OPTION_DATA_VERSION
    return FuturesOptionResult(
        symbol=symbol,
        source=source,
        data_kind="期货",
        dataframe=analyzed,
        summary=build_summary(analyzed),
    )
