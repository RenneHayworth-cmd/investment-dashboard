#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cache import load_dataset  # noqa: E402
from core.paths import OUTPUT_DIR  # noqa: E402
from services.fund_analysis import (  # noqa: E402
    FUND_ADJUST_FORWARD_ADDITIVE,
    FUND_ADJUST_FORWARD_RATIO,
    build_fund_cache_symbol,
    fetch_tickflow_fund_close,
    infer_tickflow_symbol,
)
from services.fund_rotation import (  # noqa: E402
    normalize_rotation_dataframe,
    run_ma20_timing_backtest,
)
from services.position_analysis import (  # noqa: E402
    DEFAULT_ETF_CODES,
    ETF_AKSHARE_HISTORY_CODES,
    ETF_DISPLAY_NAMES,
    ETF_TIMING_STRATEGIES,
    _fetch_exchange_fund_close,
    calculate_etf_timing_snapshot,
    filter_final_etf_rows,
    normalize_etf_base_code,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="比较ETF差值前复权与比例前复权的择时影响。")
    parser.add_argument("--count", type=int, default=5000)
    parser.add_argument(
        "--output",
        default=str(OUTPUT_DIR / "fund_adjustment_impact.md"),
        help="Markdown报告路径；同目录同时生成CSV明细。",
    )
    return parser.parse_args(argv)


def _fetch_history(
    code: str,
    *,
    adjust: str,
    api_key: str,
    count: int,
) -> pd.DataFrame:
    symbol = infer_tickflow_symbol(code)
    source = "akshare" if normalize_etf_base_code(code) in ETF_AKSHARE_HISTORY_CODES else "tickflow"
    if adjust == FUND_ADJUST_FORWARD_ADDITIVE:
        cache_symbol = build_fund_cache_symbol("fund_close", symbol, adjust)
        cached, _meta = load_dataset(
            cache_symbol,
            source,
            "fund_close_raw",
            period=f"{int(count)}_1d",
        )
        if cached is not None and not cached.empty:
            formal = filter_final_etf_rows(
                cached,
                require_current_confirmation=True,
            )
            if formal is not None and not formal.empty:
                return formal
    if normalize_etf_base_code(code) in ETF_AKSHARE_HISTORY_CODES:
        if adjust == FUND_ADJUST_FORWARD_RATIO:
            raise ValueError("东方财富/AkShare不提供与TickFlow比例前复权等价的口径")
        raw = _fetch_exchange_fund_close(
            symbol=symbol,
            count=count,
            adjust=adjust,
        )
    else:
        raw = fetch_tickflow_fund_close(
            symbol=symbol,
            api_key=api_key,
            count=count,
            adjust=adjust,
        )
    formal = filter_final_etf_rows(raw)
    if formal is None or formal.empty:
        raise ValueError("没有可用于回测的正式日线")
    return formal


def _mode_metrics(
    code: str,
    raw: pd.DataFrame,
    *,
    mode_label: str,
    ma_period: int,
    threshold_pct: float,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> dict[str, object]:
    fund = normalize_rotation_dataframe(raw, fallback_name=code)
    result = run_ma20_timing_backtest(
        fund=fund,
        ma_period=ma_period,
        threshold_pct=threshold_pct,
        initial_capital=100000.0,
        transaction_cost=0.00006,
        lot_size=100,
        start_date=start_date,
        end_date=end_date,
    )
    timing_data = fund.dataframe.rename(
        columns={"trade_date": "date", "close": "price"}
    )
    timing_data = timing_data[timing_data["date"] <= end_date]
    snapshot = calculate_etf_timing_snapshot(
        timing_data,
        ma_period=ma_period,
        threshold_pct=threshold_pct,
    )
    return {
        "代码": code,
        "名称": ETF_DISPLAY_NAMES.get(code, code),
        "参数": f"MA{ma_period}/{threshold_pct:.1f}%",
        "复权口径": mode_label,
        "开始日期": result.summary["开始日期"],
        "结束日期": result.summary["结束日期"],
        "当前信号": snapshot.get("择时判断", "-"),
        "年化收益率(%)": result.summary["年化收益率(%)"],
        "策略最大回撤(%)": result.summary["策略最大回撤(%)"],
        "交易次数": result.summary["交易次数"],
        "交易胜率(%)": result.summary["交易胜率(%)"],
        "错误": "",
    }


def audit_symbol(code: str, *, api_key: str, count: int) -> list[dict[str, object]]:
    ma_period, threshold_pct = ETF_TIMING_STRATEGIES[code]
    histories: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    for mode_label, mode in (
        ("前复权（差值）", FUND_ADJUST_FORWARD_ADDITIVE),
        ("前复权（比例）", FUND_ADJUST_FORWARD_RATIO),
    ):
        try:
            histories[mode_label] = _fetch_history(
                code,
                adjust=mode,
                api_key=api_key,
                count=count,
            )
        except Exception as exc:
            errors[mode_label] = str(exc)

    available = list(histories.values())
    if not available:
        return [
            {
                "代码": code,
                "名称": ETF_DISPLAY_NAMES.get(code, code),
                "参数": f"MA{ma_period}/{threshold_pct:.1f}%",
                "复权口径": label,
                "错误": errors.get(label, "无可用数据"),
            }
            for label in ("前复权（差值）", "前复权（比例）")
        ]

    first_dates = [pd.to_datetime(frame["日期"]).min() for frame in available]
    last_dates = [pd.to_datetime(frame["日期"]).max() for frame in available]
    start_date = max(first_dates)
    end_date = min(last_dates)
    rows = []
    for label in ("前复权（差值）", "前复权（比例）"):
        if label in errors:
            rows.append(
                {
                    "代码": code,
                    "名称": ETF_DISPLAY_NAMES.get(code, code),
                    "参数": f"MA{ma_period}/{threshold_pct:.1f}%",
                    "复权口径": label,
                    "开始日期": start_date.strftime("%Y-%m-%d"),
                    "结束日期": end_date.strftime("%Y-%m-%d"),
                    "当前信号": "-",
                    "年化收益率(%)": pd.NA,
                    "策略最大回撤(%)": pd.NA,
                    "交易次数": pd.NA,
                    "交易胜率(%)": pd.NA,
                    "错误": errors[label],
                }
            )
            continue
        rows.append(
            _mode_metrics(
                code,
                histories[label],
                mode_label=label,
                ma_period=ma_period,
                threshold_pct=threshold_pct,
                start_date=start_date,
                end_date=end_date,
            )
        )
    return rows


def write_report(results: pd.DataFrame, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path = output_path.with_suffix(".csv")
    results.to_csv(csv_path, index=False, encoding="utf-8-sig")
    display_columns = [
        "代码",
        "名称",
        "参数",
        "复权口径",
        "开始日期",
        "结束日期",
        "当前信号",
        "年化收益率(%)",
        "策略最大回撤(%)",
        "交易次数",
        "交易胜率(%)",
        "错误",
    ]
    table = results.reindex(columns=display_columns).fillna("-").to_markdown(index=False)
    output_path.write_text(
        "\n".join(
            [
                "# ETF复权口径影响审计",
                "",
                "- 口径：各ETF自身实际历史；同一标的两种复权使用共同起止日。",
                "- 参数：沿用当前ETF_TIMING_STRATEGIES，不重新优化周期、阈值、仓位或权重。",
                "- 成交：初始资金10万元，单边费用万0.6，100份交易单位，当日收盘信号按当日收盘成交。",
                "- 比例前复权仅用于复现旧结果；161128的数据源不提供等价比例口径，因此标记不可比。",
                "",
                table,
                "",
                f"CSV明细：{csv_path.name}",
            ]
        ),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if not api_key:
        print("未设置TICKFLOW_API_KEY，审计未执行。", file=sys.stderr)
        return 2
    rows = []
    codes = [code for code in DEFAULT_ETF_CODES if code in ETF_TIMING_STRATEGIES]
    for index, code in enumerate(codes, start=1):
        print(f"[{index}/{len(codes)}] 审计 {code} ...", flush=True)
        rows.extend(audit_symbol(code, api_key=api_key, count=int(args.count)))
        if index < len(codes):
            time.sleep(6.2)
    results = pd.DataFrame(rows)
    output_path = Path(args.output)
    write_report(results, output_path)
    failures = int(results.get("错误", pd.Series(dtype=str)).fillna("").ne("").sum())
    print(f"报告已生成：{output_path}")
    return 1 if failures and failures >= len(codes) * 2 else 0


if __name__ == "__main__":
    raise SystemExit(main())
