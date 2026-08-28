from __future__ import annotations

import calendar
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from services.market_calendar import (
    get_market_window,
    is_market_trading_day,
    latest_settled_trade_date,
)


CONTRACT_PREFIXES = {
    "IC": "中证500股指",
    "IF": "沪深300股指",
    "IH": "上证50股指",
    "IM": "中证1000股指",
    "TL": "30年国债",
    "T": "10年国债",
    "TF": "5年国债",
    "TS": "2年国债",
    "CU": "沪铜",
    "AL": "沪铝",
    "ZN": "沪锌",
    "PB": "沪铅",
    "NI": "沪镍",
    "SN": "沪锡",
    "AU": "沪金",
    "AG": "沪银",
    "RB": "螺纹钢",
    "HC": "热轧卷板",
    "SS": "不锈钢",
    "BU": "沥青",
    "RU": "天然橡胶",
    "FU": "燃油",
    "M": "豆粕",
    "Y": "豆油",
    "P": "棕榈油",
    "C": "玉米",
    "L": "聚乙烯",
    "V": "聚氯乙烯",
    "PP": "聚丙烯",
    "J": "焦炭",
    "JM": "焦煤",
    "I": "铁矿石",
    "CF": "棉花",
    "SR": "白糖",
    "TA": "PTA",
    "MA": "甲醇",
    "FG": "玻璃",
    "RM": "菜籽粕",
    "OI": "菜籽油",
    "SA": "纯碱",
    "SI": "工业硅",
    "LC": "碳酸锂",
}

FUTURES_EXCHANGES = {
    "CU": "SHF",
    "AL": "SHF",
    "ZN": "SHF",
    "PB": "SHF",
    "NI": "SHF",
    "SN": "SHF",
    "AU": "SHF",
    "AG": "SHF",
    "RB": "SHF",
    "HC": "SHF",
    "SS": "SHF",
    "BU": "SHF",
    "RU": "SHF",
    "FU": "SHF",
    "M": "DCE",
    "Y": "DCE",
    "P": "DCE",
    "C": "DCE",
    "L": "DCE",
    "V": "DCE",
    "PP": "DCE",
    "J": "DCE",
    "JM": "DCE",
    "I": "DCE",
    "CF": "ZCE",
    "SR": "ZCE",
    "TA": "ZCE",
    "MA": "ZCE",
    "FG": "ZCE",
    "RM": "ZCE",
    "OI": "ZCE",
    "SA": "ZCE",
    "SI": "GFE",
    "LC": "GFE",
    "IC": "CFX",
    "IF": "CFX",
    "IH": "CFX",
    "IM": "CFX",
    "TL": "CFX",
    "T": "CFX",
    "TF": "CFX",
    "TS": "CFX",
}

SPREAD_CALCULATION_VERSION = "futures_spread_v2"
logger = logging.getLogger(__name__)

_SINA_FUTURES_SPOT_URL = "https://hq.sinajs.cn/list=nf_{contract}"
_SINA_FUTURES_DAILY_URL = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
    "var%20_V21052021_4_12=/InnerFuturesNewService.getDailyKLine"
)
_SINA_HEADERS = {
    "Referer": "https://vip.stock.finance.sina.com.cn/",
    "User-Agent": "Mozilla/5.0",
}


def parse_contracts(text: str) -> list[str]:
    contracts = [item.strip().upper() for item in re.split(r"[\s,，]+", text) if item.strip()]
    return list(dict.fromkeys(contracts))


def contract_name(code: str) -> str:
    match = re.match(r"^([A-Z]+)", code.upper())
    if not match:
        return code.upper()
    name = CONTRACT_PREFIXES.get(match.group(1), "")
    return f"{code.upper()} ({name})" if name else code.upper()


def personal_investor_cutoff_date(contract: str) -> pd.Timestamp | None:
    """Return the last spread date for contracts with personal holding limits."""
    contract = contract.upper()
    match = re.match(r"^([A-Z]+)(\d{4})$", contract)
    if not match:
        return None

    prefix, yymm = match.groups()
    if prefix != "I":
        return None

    year = 2000 + int(yymm[:2])
    month = int(yymm[2:])
    if month < 1 or month > 12:
        return None

    cutoff_year = year if month > 1 else year - 1
    cutoff_month = month - 1 if month > 1 else 12
    cutoff_day = calendar.monthrange(cutoff_year, cutoff_month)[1]
    return pd.Timestamp(cutoff_year, cutoff_month, cutoff_day)


def build_cutoff_notes(contracts: list[str]) -> list[str]:
    notes = []
    for contract in contracts:
        cutoff = personal_investor_cutoff_date(contract)
        if cutoff is None:
            continue
        notes.append(f"{contract_name(contract)} 按 {cutoff.strftime('%Y-%m-%d')} 截止计算")
    return notes[:]


def spread_respects_contract_cutoffs(
    df: pd.DataFrame,
    contracts: list[str],
    base_contract: str,
) -> bool:
    if df is None or df.empty or "date" not in df.columns:
        return False

    dates = pd.to_datetime(df["date"], errors="coerce")
    for contract in contracts:
        cutoff = personal_investor_cutoff_date(contract)
        if cutoff is None:
            continue
        after_cutoff = dates > cutoff
        if not after_cutoff.any():
            continue

        related_spread_cols = []
        if contract == base_contract:
            related_spread_cols = [
                f"spread_{base_contract}_vs_{other}"
                for other in contracts
                if other != base_contract
            ]
        else:
            related_spread_cols = [f"spread_{base_contract}_vs_{contract}"]

        for col in related_spread_cols:
            if col in df.columns and df.loc[after_cutoff, col].notna().any():
                return False
    return True


def infer_tickflow_futures_symbol(contract: str) -> str:
    contract = contract.strip()
    if "." in contract:
        code, exchange = contract.rsplit(".", 1)
        return f"{code}.{exchange.upper()}"
    match = re.match(r"^([A-Za-z]+)(\d+)$", contract)
    if not match:
        raise ValueError(f"无法识别期货合约代码：{contract}")
    prefix, month = match.groups()
    exchange = FUTURES_EXCHANGES.get(prefix.upper())
    if not exchange:
        raise ValueError(f"无法推断交易所后缀：{contract}")
    code_prefix = prefix.lower() if exchange in {"DCE", "ZCE", "GFE"} else prefix.upper()
    return f"{code_prefix}{month}.{exchange}"


def _china_now(market_now: datetime | None = None) -> datetime:
    if market_now is None:
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    if market_now.tzinfo is None:
        return market_now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return market_now.astimezone(ZoneInfo("Asia/Shanghai"))


def _today_china(market_now: datetime | None = None) -> pd.Timestamp:
    return pd.Timestamp(_china_now(market_now).date())


def completed_futures_daily_cutoff(market_now: datetime | None = None) -> pd.Timestamp:
    market = get_market_window("A股")
    now = _china_now(market_now)
    if market is None:
        return pd.Timestamp(now.date()) - pd.Timedelta(days=1)
    return pd.Timestamp(
        latest_settled_trade_date(
            market,
            now,
            settlement_delay=timedelta(minutes=5),
        )
    )


def filter_completed_futures_daily(
    df: pd.DataFrame,
    *,
    market_now: datetime | None = None,
) -> pd.DataFrame:
    if df is None or df.empty or "date" not in df.columns:
        return df
    result = df.copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    cutoff = completed_futures_daily_cutoff(market_now)
    return result[result["date"].dt.normalize() <= cutoff].reset_index(drop=True)


def _spot_market_for_contract(contract: str) -> str:
    match = re.match(r"^([A-Za-z]+)", contract.strip())
    if not match:
        return "CF"
    exchange = FUTURES_EXCHANGES.get(match.group(1).upper(), "")
    return "CFFEX" if exchange == "CFX" else "CF"


def _sina_get_without_environment_proxy(
    url: str,
    *,
    params: dict[str, str] | None = None,
):
    """Use the same Sina source without inheriting a dead desktop proxy."""
    import requests

    session = requests.Session()
    session.trust_env = False
    response = session.get(
        url,
        params=params,
        headers=_SINA_HEADERS,
        timeout=10,
    )
    response.raise_for_status()
    return response


def _fetch_futures_spot_from_sina_direct(contract: str) -> pd.DataFrame:
    contract = contract.strip().upper()
    response = _sina_get_without_environment_proxy(
        _SINA_FUTURES_SPOT_URL.format(contract=contract)
    )
    text = response.content.decode("gb18030", errors="replace")
    match = re.search(
        rf'hq_str_nf_{re.escape(contract)}="([^"]*)"',
        text,
        flags=re.IGNORECASE,
    )
    if match is None or not match.group(1):
        raise ValueError("新浪期货实时接口未返回合约数据")

    values = match.group(1).split(",")
    if _spot_market_for_contract(contract) == "CFFEX":
        price_index, date_index, time_index = 3, 36, 37
    else:
        price_index, date_index, time_index = 8, 17, 1
    if len(values) <= max(price_index, date_index, time_index):
        raise ValueError("新浪期货实时接口字段数量异常")

    return pd.DataFrame(
        [
            {
                "current_price": values[price_index],
                "quote_date": values[date_index],
                "quote_time": values[time_index],
            }
        ]
    )


def _fetch_futures_daily_from_sina_direct(contract: str) -> pd.DataFrame:
    response = _sina_get_without_environment_proxy(
        _SINA_FUTURES_DAILY_URL,
        params={"symbol": contract.strip().upper(), "type": "2021_04_12"},
    )
    text = response.text
    if "=(" not in text or ");" not in text:
        raise ValueError("新浪期货日线接口响应格式异常")
    payload = json.loads(text.split("=(", 1)[1].split(");", 1)[0])
    raw = pd.DataFrame(payload)
    if raw.empty:
        raise ValueError("新浪期货日线接口返回空数据")
    if {"d", "c"}.issubset(raw.columns):
        raw = raw.rename(columns={"d": "date", "c": "close"})
    elif len(raw.columns) == 8:
        raw.columns = [
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "hold",
            "settle",
        ]
    return normalize_futures_daily(raw)


def append_futures_spot_row(
    df: pd.DataFrame,
    contract: str,
    *,
    replace_current_day: bool = False,
    market_now: datetime | None = None,
) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    result = df.copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    now = _china_now(market_now)
    today = _today_china(now)
    market = get_market_window("A股")
    if market is None or not is_market_trading_day(market, now):
        return result

    latest_history_date = result["date"].dropna().max()
    if (
        not replace_current_day
        and pd.notna(latest_history_date)
        and latest_history_date.normalize() >= today
    ):
        return result

    try:
        import akshare as ak

        spot_df = ak.futures_zh_spot(
            symbol=contract.strip().upper(),
            market=_spot_market_for_contract(contract),
            adjust="0",
        )
    except Exception as akshare_exc:
        try:
            spot_df = _fetch_futures_spot_from_sina_direct(contract)
        except Exception as direct_exc:
            logger.warning(
                "期货实时行情获取失败，合约=%s，AkShare=%s，新浪直连=%s",
                contract,
                akshare_exc,
                direct_exc,
            )
            return result

    if spot_df is None or spot_df.empty:
        return result

    price_col = None
    for candidate in ("current_price", "最新价", "price", "last_close"):
        if candidate in spot_df.columns:
            price_col = candidate
            break
    if price_col is None:
        return result

    if "quote_date" in spot_df.columns:
        quote_date = pd.to_datetime(
            spot_df.iloc[0]["quote_date"], errors="coerce"
        )
        if pd.isna(quote_date) or quote_date.normalize() != today:
            return result

    latest_price = pd.to_numeric(spot_df.iloc[0][price_col], errors="coerce")
    if pd.isna(latest_price) or latest_price <= 0:
        return result

    supplement = pd.DataFrame([{"date": today, "close": float(latest_price)}])
    return (
        pd.concat([result, supplement], ignore_index=True)
        .sort_values("date")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )


def normalize_futures_daily(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        raise ValueError("返回空数据")
    if "trade_date" in df.columns:
        date_col = "trade_date"
    elif "date" in df.columns:
        date_col = "date"
    else:
        raise ValueError(f"返回列无法识别：{list(df.columns)}")
    if "close" not in df.columns:
        raise ValueError(f"返回列无法识别：{list(df.columns)}")

    result = df[[date_col, "close"]].copy()
    result.columns = ["date", "close"]
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    result = result.dropna(subset=["date", "close"]).sort_values("date")
    if result.empty:
        raise ValueError("日期或收盘价解析后为空")
    return result


def fetch_futures_daily_from_tickflow(contract: str, api_key: str = "") -> pd.DataFrame:
    from tickflow import TickFlow

    symbol = infer_tickflow_futures_symbol(contract)
    client = TickFlow(api_key=api_key) if api_key else TickFlow.free()
    df = client.klines.get(symbol, period="1d", count=5000, as_dataframe=True)
    return normalize_futures_daily(df)


def fetch_futures_daily_from_akshare(contract: str) -> pd.DataFrame:
    import akshare as ak

    try:
        df = ak.futures_zh_daily_sina(symbol=contract)
        return normalize_futures_daily(df)
    except Exception as akshare_exc:
        try:
            return _fetch_futures_daily_from_sina_direct(contract)
        except Exception as direct_exc:
            raise RuntimeError(
                f"AkShare期货日线失败：{akshare_exc}；新浪直连失败：{direct_exc}"
            ) from direct_exc


def fetch_futures_daily(
    contract: str,
    api_key: str = "",
    *,
    prefer_realtime_snapshot: bool = False,
    market_now: datetime | None = None,
) -> pd.DataFrame:
    tickflow_df = None
    tickflow_error = None
    try:
        tickflow_df = fetch_futures_daily_from_tickflow(contract, api_key=api_key)
    except Exception as exc:
        tickflow_error = exc

    try:
        akshare_df = fetch_futures_daily_from_akshare(contract)
    except Exception:
        if tickflow_df is not None:
            selected = tickflow_df
            if prefer_realtime_snapshot:
                return append_futures_spot_row(
                    selected,
                    contract,
                    replace_current_day=True,
                    market_now=market_now,
                )
            return filter_completed_futures_daily(selected, market_now=market_now)
        if tickflow_error is not None:
            raise tickflow_error
        raise

    if tickflow_df is None:
        selected = akshare_df
    else:
        tickflow_latest = tickflow_df["date"].max()
        akshare_latest = akshare_df["date"].max()
        selected = (
            akshare_df
            if pd.notna(akshare_latest)
            and pd.notna(tickflow_latest)
            and akshare_latest > tickflow_latest
            else tickflow_df
        )

    if prefer_realtime_snapshot:
        return append_futures_spot_row(
            selected,
            contract,
            replace_current_day=True,
            market_now=market_now,
        )
    return filter_completed_futures_daily(selected, market_now=market_now)


def fetch_contracts(
    contracts: list[str],
    max_workers: int = 5,
    api_key: str = "",
    *,
    prefer_realtime_snapshot: bool = False,
    market_now: datetime | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    data: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    workers = max(1, min(max_workers, len(contracts)))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                fetch_futures_daily,
                contract,
                api_key=api_key,
                prefer_realtime_snapshot=prefer_realtime_snapshot,
                market_now=market_now,
            ): contract
            for contract in contracts
        }
        for future in as_completed(futures):
            contract = futures[future]
            try:
                data[contract] = future.result()
            except Exception as exc:
                errors.append(f"{contract_name(contract)}: {exc}")

    return data, errors


def calculate_spreads(data: dict[str, pd.DataFrame], base_contract: str) -> pd.DataFrame:
    if base_contract not in data:
        raise ValueError(f"基准合约 {base_contract} 没有可用数据。")

    merged = None
    for contract, df in data.items():
        renamed = df.rename(columns={"close": f"{contract}_close"})
        if merged is None:
            merged = renamed
        else:
            merged = pd.merge(merged, renamed, on="date", how="outer")

    if merged is None or merged.empty:
        raise ValueError("没有可计算的数据。")

    merged = merged.sort_values("date").reset_index(drop=True)
    base_col = f"{base_contract}_close"
    effective_last_dates = {}
    for contract, df in data.items():
        last_date = pd.to_datetime(df["date"], errors="coerce").max()
        cutoff = personal_investor_cutoff_date(contract)
        if cutoff is not None and pd.notna(last_date):
            last_date = min(last_date, cutoff)
        elif cutoff is not None:
            last_date = cutoff
        effective_last_dates[contract] = last_date

    for contract in data:
        close_col = f"{contract}_close"
        merged[close_col] = merged[close_col].ffill()
        last_date = effective_last_dates.get(contract)
        if pd.notna(last_date):
            merged.loc[merged["date"] > last_date, close_col] = pd.NA

    spread_cols = []
    for contract in data:
        if contract == base_contract:
            continue
        spread_col = f"spread_{base_contract}_vs_{contract}"
        pct_col = f"{spread_col}_pct"
        spread_cols.append(spread_col)
        merged[spread_col] = merged[base_col] - merged[f"{contract}_close"]
        merged[pct_col] = merged[spread_col] / merged[base_col] * 100

    if spread_cols:
        merged = merged.dropna(how="all", subset=spread_cols).reset_index(drop=True)

    merged["_calculation_version"] = SPREAD_CALCULATION_VERSION
    numeric_cols = merged.select_dtypes(include="number").columns
    merged[numeric_cols] = merged[numeric_cols].round(4)
    return merged


def build_spread_summary(df: pd.DataFrame, contracts: list[str], base_contract: str) -> pd.DataFrame:
    rows = []
    for contract in contracts:
        if contract == base_contract:
            continue
        spread_col = f"spread_{base_contract}_vs_{contract}"
        pct_col = f"{spread_col}_pct"
        if spread_col not in df.columns:
            continue
        spread = df[spread_col].dropna()
        pct = df[pct_col].dropna()
        if spread.empty:
            continue
        rows.append(
            {
                "价差对": f"{contract_name(base_contract)} - {contract_name(contract)}",
                "最新价差": spread.iloc[-1],
                "最新占比(%)": pct.iloc[-1] if not pct.empty else float("nan"),
                "平均价差": spread.mean(),
                "最大价差": spread.max(),
                "最小价差": spread.min(),
                "样本数": len(spread),
            }
        )
    return pd.DataFrame(rows).round(4)
