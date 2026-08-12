from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
import math

import pandas as pd

from core.db import get_conn, init_db


DATE_COLUMNS = ("trade_date", "日期", "date", "datetime", "time", "净值日期")
PRICE_COLUMNS = ("close", "收盘价", "收盘", "累计净值", "复权净值", "单位净值", "nav", "price")


@dataclass
class CorrelationInput:
    symbol: str
    name: str
    dataframe: pd.DataFrame


@dataclass
class CorrelationResult:
    aligned_prices: pd.DataFrame
    correlation_matrix: pd.DataFrame
    pair_table: pd.DataFrame
    summary: dict[str, object]


def parse_symbols(text: str) -> list[str]:
    items = [
        item.strip()
        for item in text.replace(",", " ").replace("，", " ").replace(";", " ").replace("；", " ").replace("\n", " ").split()
        if item.strip()
    ]
    return list(dict.fromkeys(items))


def normalize_price_dataframe(df: pd.DataFrame, fallback_name: str, fallback_symbol: str | None = None) -> CorrelationInput:
    if df is None or df.empty:
        raise ValueError("没有可分析的数据。")

    data = df.copy()
    data.columns = [str(column).strip().lstrip("\ufeff") for column in data.columns]
    date_col = _find_column(data.columns, DATE_COLUMNS)
    price_col = _find_column(data.columns, PRICE_COLUMNS)
    if not date_col or not price_col:
        raise ValueError(f"无法识别日期列或收盘价列。当前列名：{list(data.columns)}")

    symbol = _first_text(data, ("symbol", "代码", "基金代码")) or fallback_symbol or fallback_name
    name = _first_text(data, ("name", "基金名称", "名称", "简称")) or fallback_name or symbol
    normalized = data[[date_col, price_col]].copy()
    normalized.columns = ["date", "close"]
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    normalized["close"] = pd.to_numeric(normalized["close"], errors="coerce")
    normalized = normalized.dropna(subset=["date", "close"])
    normalized = normalized.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    if normalized.empty:
        raise ValueError("日期和收盘价解析后没有有效数据。")

    return CorrelationInput(symbol=str(symbol), name=str(name), dataframe=normalized)


def calculate_price_correlation(items: list[CorrelationInput], method: str = "price") -> CorrelationResult:
    if len(items) < 2:
        raise ValueError("至少需要 2 个标的才能计算相关系数。")

    aligned = None
    labels: list[str] = []
    for item in items:
        label = _unique_label(_display_label(item), labels)
        labels.append(label)
        current = item.dataframe[["date", "close"]].copy()
        current = current.rename(columns={"close": label})
        aligned = current if aligned is None else pd.merge(aligned, current, on="date", how="inner")

    if aligned is None or aligned.empty:
        raise ValueError("不同标的没有共同交易日期，无法计算相关系数。")

    aligned = aligned.sort_values("date").dropna().reset_index(drop=True)
    if len(aligned) < 2:
        raise ValueError("时间对齐后共同数据不足 2 行，无法计算相关系数。")

    price_frame = aligned.set_index("date")
    if method == "return":
        calculation_frame = price_frame.pct_change().replace([float("inf"), float("-inf")], pd.NA).dropna()
        if len(calculation_frame) < 2:
            raise ValueError("日收益率数据不足 2 行，无法计算相关系数。")
    else:
        calculation_frame = price_frame

    matrix = calculation_frame.corr(method="pearson").round(4)
    pair_table = _build_pair_table(matrix)
    summary = _build_summary(calculation_frame, pair_table)
    summary["计算方式"] = "日收益率相关" if method == "return" else "收盘价相关"
    return CorrelationResult(
        aligned_prices=price_frame.reset_index(),
        correlation_matrix=matrix,
        pair_table=pair_table,
        summary=summary,
    )


def describe_correlation(value: float) -> str:
    numeric = float(value)
    if not math.isfinite(numeric):
        return "无法计算"
    abs_value = abs(numeric)
    if abs_value < 0.2:
        strength = "很弱"
    elif abs_value < 0.4:
        strength = "较弱"
    elif abs_value < 0.6:
        strength = "中等"
    elif abs_value < 0.8:
        strength = "较强"
    else:
        strength = "很强"
    if numeric < 0:
        return f"负相关，{strength}"
    return strength


def save_correlation_results(pair_table: pd.DataFrame, summary: dict[str, object], source_summary: str = "") -> None:
    if pair_table is None or pair_table.empty:
        return

    init_db()
    rows = []
    created_at = datetime.now().isoformat(timespec="seconds")
    for _, row in pair_table.iterrows():
        value = float(row["相关系数r"])
        if not math.isfinite(value):
            continue
        rows.append(
            (
                str(row["标的A"]),
                str(row["标的B"]),
                value,
                describe_correlation(value),
                str(summary.get("开始日期", "")),
                str(summary.get("结束日期", "")),
                int(summary.get("共同日期数", 0) or 0),
                source_summary,
                created_at,
            )
        )

    if not rows:
        return

    with closing(get_conn()) as conn:
        conn.executemany(
            """
            INSERT INTO correlation_results (
                asset_a, asset_b, correlation, strength, start_date, end_date,
                common_days, source_summary, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()


def list_correlation_results(limit: int = 200) -> pd.DataFrame:
    init_db()
    with closing(get_conn()) as conn:
        df = pd.read_sql_query(
            """
            SELECT
                id,
                asset_a AS 标的A,
                asset_b AS 标的B,
                correlation AS 相关系数r,
                strength AS 相关性,
                start_date AS 开始日期,
                end_date AS 结束日期,
                common_days AS 共同日期数,
                source_summary AS 计算说明,
                created_at AS 计算时间
            FROM correlation_results
            ORDER BY id DESC
            LIMIT ?
            """,
            conn,
            params=(limit,),
        )
    if not df.empty:
        df["相关系数r"] = pd.to_numeric(df["相关系数r"], errors="coerce").round(4)
    return df


def delete_correlation_results(ids: list[int]) -> None:
    clean_ids = [int(item) for item in ids if item is not None]
    if not clean_ids:
        return
    init_db()
    placeholders = ",".join("?" for _ in clean_ids)
    with closing(get_conn()) as conn:
        conn.execute(f"DELETE FROM correlation_results WHERE id IN ({placeholders})", clean_ids)
        conn.commit()


def _find_column(columns, keywords: tuple[str, ...]) -> str | None:
    normalized = [str(column).strip().lstrip("\ufeff") for column in columns]
    for keyword in keywords:
        keyword_lower = keyword.lower()
        for column in normalized:
            if keyword_lower in column.lower():
                return column
    return None


def _first_text(df: pd.DataFrame, columns: tuple[str, ...]) -> str | None:
    for column in columns:
        if column in df.columns and df[column].notna().any():
            value = str(df[column].dropna().iloc[0]).strip()
            if value:
                return value
    return None


def _unique_label(label: str, existing: list[str]) -> str:
    clean_label = label or "标的"
    if clean_label not in existing:
        return clean_label
    index = 2
    while f"{clean_label}_{index}" in existing:
        index += 1
    return f"{clean_label}_{index}"


def _display_label(item: CorrelationInput) -> str:
    name = str(item.name).strip()
    symbol = str(item.symbol).strip()
    if not name:
        return symbol
    if not symbol or name == symbol:
        return name
    return f"{name} {symbol}".strip()


def _build_pair_table(matrix: pd.DataFrame) -> pd.DataFrame:
    rows = []
    columns = list(matrix.columns)
    for left_index, left in enumerate(columns):
        for right in columns[left_index + 1 :]:
            value = pd.to_numeric(matrix.loc[left, right], errors="coerce")
            rows.append(
                {
                    "标的A": left,
                    "标的B": right,
                    "相关系数r": value,
                    "相关性": describe_correlation(value),
                }
            )
    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.sort_values("相关系数r", ascending=False).reset_index(drop=True)
    return result


def _build_summary(price_frame: pd.DataFrame, pair_table: pd.DataFrame) -> dict[str, object]:
    summary: dict[str, object] = {
        "标的数量": len(price_frame.columns),
        "共同日期数": len(price_frame),
        "开始日期": pd.Timestamp(price_frame.index.min()).strftime("%Y-%m-%d"),
        "结束日期": pd.Timestamp(price_frame.index.max()).strftime("%Y-%m-%d"),
    }
    if pair_table.empty:
        summary["平均相关系数r"] = "-"
        summary["最高相关"] = "-"
        summary["最低相关"] = "-"
        return summary

    values = pd.to_numeric(pair_table["相关系数r"], errors="coerce")
    finite_pairs = pair_table[values.map(lambda value: pd.notna(value) and math.isfinite(float(value)))]
    values = pd.to_numeric(finite_pairs["相关系数r"], errors="coerce")
    summary["平均相关系数r"] = round(float(values.mean()), 4) if not values.empty else "-"
    if finite_pairs.empty:
        summary["最高相关"] = "-"
        summary["最低相关"] = "-"
        return summary
    top = finite_pairs.iloc[0]
    bottom = finite_pairs.iloc[-1]
    summary["最高相关"] = f"{top['标的A']} / {top['标的B']}：{top['相关系数r']}"
    summary["最低相关"] = f"{bottom['标的A']} / {bottom['标的B']}：{bottom['相关系数r']}"
    return summary
