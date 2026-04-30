from __future__ import annotations

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


def parse_contracts(text: str) -> list[str]:
    contracts = [item.strip().upper() for item in re.split(r"[\s,，]+", text) if item.strip()]
    return list(dict.fromkeys(contracts))


def contract_name(code: str) -> str:
    match = re.match(r"^([A-Z]+)", code.upper())
    if not match:
        return code.upper()
    name = CONTRACT_PREFIXES.get(match.group(1), "")
    return f"{code.upper()} ({name})" if name else code.upper()


def fetch_futures_daily(contract: str) -> pd.DataFrame:
    import akshare as ak

    df = ak.futures_zh_daily_sina(symbol=contract)
    if df is None or df.empty:
        raise ValueError("返回空数据")
    if "date" not in df.columns or "close" not in df.columns:
        raise ValueError(f"返回列无法识别：{list(df.columns)}")

    result = df[["date", "close"]].copy()
    result["date"] = pd.to_datetime(result["date"], errors="coerce")
    result["close"] = pd.to_numeric(result["close"], errors="coerce")
    result = result.dropna(subset=["date", "close"]).sort_values("date")
    if result.empty:
        raise ValueError("日期或收盘价解析后为空")
    return result


def fetch_contracts(contracts: list[str], max_workers: int = 5) -> tuple[dict[str, pd.DataFrame], list[str]]:
    data: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    workers = max(1, min(max_workers, len(contracts)))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_futures_daily, contract): contract for contract in contracts}
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
    for contract in data:
        close_col = f"{contract}_close"
        merged[close_col] = merged[close_col].ffill()

    for contract in data:
        if contract == base_contract:
            continue
        spread_col = f"spread_{base_contract}_vs_{contract}"
        pct_col = f"{spread_col}_pct"
        merged[spread_col] = merged[base_col] - merged[f"{contract}_close"]
        merged[pct_col] = merged[spread_col] / merged[base_col] * 100

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
