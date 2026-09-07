# -*- coding: utf-8 -*-
"""一次性手册数据脚本：用项目自身代码在真实缓存上重放回测。

用法（WSL，任意目录）：
    cd /mnt/c/Users/Renne/Documents/investment-dashboard
    .venv/bin/python docs/handbook/repro_hb_backtest.py
输出：output/handbook_backtest_results.json
"""
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根

import core.paths as cp

_rt = Path("/mnt/c/Users/Renne/investment_dashboard_data")
cp.RUNTIME_DIR = _rt
cp.DATA_DIR = _rt / "data"
cp.RAW_DIR = _rt / "data" / "raw"
cp.PROCESSED_DIR = _rt / "data" / "processed"
cp.OUTPUT_DIR = _rt / "output"
cp.DB_PATH = _rt / "cache.db"

# cache.db 里记录的 file_path 是 Windows 形式路径，在 WSL 下不存在；
# 包装 load_dataset：原实现读不到时按当前 RAW_DIR 直接重读 CSV。
import core.cache as _cache

_orig_load_dataset = _cache.load_dataset


def _load_dataset_translated(symbol, source, data_type, period="1d"):
    df, meta = _orig_load_dataset(symbol, source, data_type, period)
    if df is None:
        candidate = cp.RAW_DIR / source / f"{symbol}_{period}.csv"
        if candidate.exists():
            df = pd.read_csv(candidate)
    return df, meta


_cache.load_dataset = _load_dataset_translated

from core.cache import load_dataset  # noqa: E402
from services.fund_rotation import (  # noqa: E402
    build_standard_backtest_periods,
    normalize_rotation_dataframe,
    run_ma20_timing_backtest,
    run_portfolio_timing_backtest,
    PortfolioTimingAllocation,
)
from services import position_analysis as pos  # noqa: E402

SH = ZoneInfo("Asia/Shanghai")
FAKE_NOW = datetime(2026, 9, 3, 10, 0, 0, tzinfo=SH)
OUT = Path("/mnt/c/Users/Renne/Documents/investment-dashboard/output/handbook_backtest_results.json")

CODES = ["512890", "159201", "159545", "513260", "159655", "159501",
         "161128", "518850", "588000", "159915", "510500", "159967",
         "159552", "513310", "513880"]

results = {"generated_at": datetime.now().isoformat(timespec="seconds"),
           "data_cutoff_note": "ETF缓存截至2026-09-02；指数缓存截至2026-08-14"}


def cache_key(code: str):
    sym = code + (".SH" if code.startswith(("5", "6")) else ".SZ")
    source = "akshare" if code == "161128" else "tickflow"
    return f"fund_close_v2_{sym}_forward_additive", source


def load_fund(code: str):
    key, source = cache_key(code)
    df, meta = load_dataset(key, source, "fund_close_raw", period="5000_1d")
    if df is None:
        raise RuntimeError(f"{code} 缓存缺失")
    return df, (meta or {})


def jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, datetime)):
        return str(obj)
    if isinstance(obj, float) and pd.isna(obj):
        return None
    if hasattr(obj, "item"):
        return obj.item()
    return obj


# ---------- 第1节：逐ETF参数回测 ----------
try:
    per_etf = []
    for code in CODES:
        try:
            df, meta = load_fund(code)
            name = pos.display_etf_name(code, code)
            strategy = pos.ETF_TIMING_STRATEGIES.get(code)
            fund = normalize_rotation_dataframe(df, name)
            entry = {"code": code, "name": name,
                     "rows": int(len(fund.dataframe)),
                     "first_date": str(fund.dataframe["trade_date"].min().date()),
                     "last_date": str(fund.dataframe["trade_date"].max().date()),
                     "cache_time": meta.get("last_update_time", "")}
            if strategy is not None:
                ma, thr = int(strategy[0]), float(strategy[1])
                entry["ma_period"], entry["threshold_pct"] = ma, thr
            else:
                ma, thr = 20, 1.0
                entry["ma_period"], entry["threshold_pct"] = ma, thr
                entry["note"] = "停车ETF无自身参数，此处用MA20/1%作示意基准"
            fund.symbol = code
            fund.name = name
            end = fund.dataframe["trade_date"].max()
            for label, start in build_standard_backtest_periods(end):
                if label == "近五年":
                    continue
                try:
                    r = run_ma20_timing_backtest(
                        fund, ma_period=ma, threshold_pct=thr,
                        initial_capital=100000.0, transaction_cost=0.00006,
                        lot_size=100, start_date=start, end_date=end)
                    entry[label] = r.summary
                    if label == "成立来":
                        entry["yearly"] = r.yearly_stats.to_dict("records")
                except Exception as exc:
                    entry[label] = {"error": str(exc)}
            per_etf.append(entry)
            print("per-etf done:", code, flush=True)
        except Exception:
            per_etf.append({"code": code, "error": traceback.format_exc()})
    results["per_etf_timing"] = per_etf
except Exception:
    results["per_etf_timing_error"] = traceback.format_exc()

# ---------- 第2节：冻结10ETF组合回测 ----------
try:
    frozen_alloc = [
        ("512890", "红利低波ETF华泰柏瑞", 10.0, "hold", 20, 1.0),
        ("159201", "自由现金流ETF华夏", 10.0, "timing", 20, 0.5),
        ("513260", "恒生科技ETF汇添富", 10.0, "timing", 20, 1.0),
        ("588000", "科创50ETF华夏", 10.0, "timing", 20, 1.0),
        ("510500", "中证500ETF南方", 10.0, "timing", 15, 1.0),
        ("159967", "创业板成长ETF华夏", 10.0, "timing", 25, 2.0),
        ("159545", "恒生红利低波ETF易方达", 10.0, "timing", 10, 1.0),
        ("159501", "纳指ETF嘉实", 10.0, "half_timing", 25, 2.0),
        ("159655", "标普500ETF华夏", 10.0, "half_timing", 25, 2.0),
        ("518850", "黄金ETF华夏", 10.0, "timing", 30, 1.5),
    ]
    funds = []
    for code, *_ in frozen_alloc:
        df, _ = load_fund(code)
        fund = normalize_rotation_dataframe(df, pos.display_etf_name(code, code))
        fund.symbol = code
        fund.name = pos.display_etf_name(code, code)
        funds.append(fund)
    allocations = [
        PortfolioTimingAllocation(symbol=c, name=n, weight_pct=w, strategy=s,
                                  ma_period=m, threshold_pct=t)
        for c, n, w, s, m, t in frozen_alloc
    ]
    r = run_portfolio_timing_backtest(funds, allocations, initial_capital=100000.0,
                                      transaction_cost=0.00006, lot_size=100)
    nav = r.nav_data
    results["frozen_portfolio"] = {
        "summary": r.summary,
        "component_results": r.component_results.to_dict("records"),
        "yearly_stats": r.yearly_stats.to_dict("records"),
        "trades_count": int(len(r.trades)),
        "trades_head": r.trades.head(15).to_dict("records") if not r.trades.empty else [],
        "nav_first": nav.head(3).to_dict("records"),
        "nav_last": nav.tail(3).to_dict("records"),
        "common_start": str(r.start_date.date()), "common_end": str(r.end_date.date()),
    }
    print("frozen portfolio done", flush=True)
except Exception:
    results["frozen_portfolio_error"] = traceback.format_exc()

# ---------- 第3节：50万元固定策略复现 ----------
try:
    codes11 = ["512890", "159201", "159545", "513260", "159655", "159501",
               "518850", "510500", "159967", "159552", "513310", "513880"]
    # 注意 50万策略权重ETF = 159201,159545,159655,159501,518850,510500,159967,159552,513310,513880 + 512890
    codes11 = ["512890", "159201", "159545", "159655", "159501", "518850",
               "510500", "159967", "159552", "513310", "513880"]
    items = []
    for code in codes11:
        item = pos.load_or_fetch_etf(code, api_key="", count=5000, adjust="forward_additive",
                                     allow_fetch=False, save_to_cache=False, market_now=FAKE_NOW)
        items.append(item)
    perf = pos.build_position_timing_performance(items, market_now=FAKE_NOW)
    daily = perf.daily
    section = {
        "summary": perf.summary,
        "warnings": perf.warnings,
        "errors": perf.errors,
        "trades_count": int(len(perf.trades)),
        "trades_all": perf.trades.to_dict("records") if len(perf.trades) <= 60 else perf.trades.head(60).to_dict("records"),
        "daily_tail": daily.tail(8).to_dict("records"),
        "daily_head": daily.head(5).to_dict("records"),
    }
    try:
        parking = pos.calculate_512890_parking_snapshot(items)
        section["parking_512890"] = {k: (str(v) if isinstance(v, pd.Timestamp) else v)
                                     for k, v in parking.items()}
    except Exception:
        section["parking_512890_error"] = traceback.format_exc()
    results["fixed_500k_strategy"] = section
    print("500k strategy done", flush=True)
except Exception:
    results["fixed_500k_error"] = traceback.format_exc()

# ---------- 第4节：指数MA20快照（用收盘确认缓存复算） ----------
try:
    idx_dir = _rt / "data" / "raw" / "index_final_history"
    rows = []
    for path in sorted(idx_dir.glob("index_raw_*_1d.csv")):
        name = path.stem[len("index_raw_"):-len("_1d")]
        df = pd.read_csv(path)
        if df.empty or "trade_date" not in df.columns or "close" not in df.columns:
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["trade_date", "close"]).sort_values("trade_date")
        df["ma20"] = df["close"].rolling(20).mean()
        # 复刻 position_timing 的带状状态机（阈值取项目通用展示口径：偏离率符号即状态）
        above = df["close"] > df["ma20"]
        state = 0
        transitions = []
        for d, c, m, a in zip(df["trade_date"], df["close"], df["ma20"], above):
            if pd.isna(m):
                continue
            desired = 1 if a else 0
            if desired != state:
                transitions.append({"date": str(d.date()), "state": desired, "price": float(c)})
                state = desired
        last = df.iloc[-1]
        t_last = transitions[-1] if transitions else None
        t_prev = transitions[-2] if len(transitions) > 1 else None
        rows.append({
            "index": name,
            "rows": int(len(df)),
            "first_date": str(df["trade_date"].iloc[0].date()),
            "last_date": str(last["trade_date"].date()),
            "close": round(float(last["close"]), 2),
            "ma20": round(float(last["ma20"]), 2),
            "deviation_pct": round((float(last["close"]) / float(last["ma20"]) - 1) * 100, 2),
            "state": "MA上方" if bool(last["close"] > last["ma20"]) else "MA下方",
            "latest_transition": t_last,
            "interval_return_pct": (round((float(last["close"]) / t_last["price"] - 1) * 100, 2)
                                    if t_last else None),
            "previous_transition": t_prev,
            "previous_interval_return_pct": (round((t_last["price"] / t_prev["price"] - 1) * 100, 2)
                                             if t_last and t_prev else None),
            "transition_count": len(transitions),
        })
    results["index_ma20_snapshot"] = rows
    print("index snapshot done", flush=True)
except Exception:
    results["index_snapshot_error"] = traceback.format_exc()

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(jsonable(results), ensure_ascii=False, indent=1, default=str),
               encoding="utf-8")
print("WROTE", OUT, flush=True)
