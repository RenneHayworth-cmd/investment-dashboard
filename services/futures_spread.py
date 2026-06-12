from __future__ import annotations

import calendar
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd


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

    df = ak.futures_zh_daily_sina(symbol=contract)
    return normalize_futures_daily(df)


def fetch_futures_daily(contract: str, api_key: str = "") -> pd.DataFrame:
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
            return tickflow_df
        if tickflow_error is not None:
            raise tickflow_error
        raise

    if tickflow_df is None:
        return akshare_df

    tickflow_latest = tickflow_df["date"].max()
    akshare_latest = akshare_df["date"].max()
    if pd.notna(akshare_latest) and pd.notna(tickflow_latest) and akshare_latest > tickflow_latest:
        return akshare_df
    return tickflow_df


def fetch_contracts(
    contracts: list[str],
    max_workers: int = 5,
    api_key: str = "",
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    data: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    workers = max(1, min(max_workers, len(contracts)))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_futures_daily, contract, api_key=api_key): contract
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
