#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build high-fidelity microcap history extension metrics (2026-04-01 to 2026-06-18)
incorporating precise ST exits and suspension filters.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path("/home/renne/investment_dashboard")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tickflow import TickFlow

# 1. Target range
START_DATE = "2026-04-01"
END_DATE = "2026-06-18"

# 2. Precise ST Ledger (Date when stock officially took effect as ST and exited microcap)
ST_LEDGER = {
    "600543": "2026-04-27",  # *ST莫高 (停牌: 2026-04-24)
    "603729": "2026-04-30",  # ST龙韵 (停牌: 2026-04-29)
    "002717": "2026-04-30",  # *ST岭南 (停牌: 2026-04-29)
    "002883": "2026-05-06",  # *ST中设 (停牌: 2026-04-30)
    "002193": "2026-05-06",  # ST如意 (停牌: 2026-04-30)
    "002856": "2026-05-06",  # *ST美芝 (停牌: 2026-04-30)
    "002719": "2026-05-06",  # ST麦趣 (停牌: 2026-04-30)
    "688121": "2026-07-07",  # *ST卓然 (5-06至7-06停牌2个月自动过滤)
}

def to_tickflow_symbol(c: str) -> str:
    code = str(c).strip().zfill(6)
    return f"{code}.SH" if code.startswith(("6", "9")) else f"{code}.SZ"

def run_build_history_extension():
    raise RuntimeError(
        "旧历史扩展已停用：固定候选池、当前股本及硬编码ST日期不满足PIT。"
        "Gate 1A通过前禁止向无真实验证区间批量扩展。"
    )

    # Retained temporarily for audit of existing legacy artifacts; unreachable.
    out_dir = ROOT / "data" / "raw" / "eastmoney" / "reconstructed"
    out_dir.mkdir(parents=True, exist_ok=True)
    ext_file = out_dir / "microcap_history_extension_20260401_20260618.csv"
    details_file = out_dir / "microcap_history_extension_daily_top20_stocks.csv"

    print("=" * 75)
    print(f"🚀 开始构建微盘股（BK1158）长周期历史回溯扩展时序: {START_DATE} ~ {END_DATE}")
    print("=" * 75)

    # 获取市场实际交易日历
    df_300 = pd.read_csv(ROOT / "data/raw/index_history/index_raw_000300.SH_1d.csv")
    d_col = [c for c in df_300.columns if "date" in c.lower() or "日期" in c][0]
    all_trading_days = sorted(pd.to_datetime(df_300[d_col]).dt.strftime("%Y-%m-%d").unique())
    target_dates = [d for d in all_trading_days if d >= START_DATE and d <= END_DATE]
    print(f"区间实际交易日总数: {len(target_dates)} 天")

    # 构建全面候选股票池
    df_snap = pd.read_csv(ROOT / "data/raw/eastmoney/microcap_bk1158_constituent_snapshots_1d.csv")
    snap_stocks = df_snap[df_snap["快照日期"] == "2026-06-22"]["代码"].astype(str).str.zfill(6).tolist()
    local_files = list((ROOT / "data/raw/eastmoney").glob("microcap_stock_daily_*_1d.csv"))
    local_stocks = [f.stem.replace("microcap_stock_daily_", "").replace("_1d", "").zfill(6) for f in local_files]
    st_stocks = list(ST_LEDGER.keys())
    all_candidates = sorted(set(snap_stocks + local_stocks + st_stocks))
    symbols = [to_tickflow_symbol(c) for c in all_candidates]
    print(f"构建候选池独立标的总数: {len(all_candidates)} 只 (包含 6月22日快照池、本地池及 4~5 月重点 ST 股)")

    # 批量拉取 TickFlow 数据
    client = TickFlow.free()
    print("正在批量获取标的元数据 (总股本)...")
    t0 = time.time()
    inst_list = client.instruments.batch(symbols)
    inst_map = {
        r["symbol"]: (r.get("name", ""), r.get("ext", {}).get("total_shares", 0.0))
        for r in inst_list
    }
    t1 = time.time()
    print(f"标的信息获取完毕，耗时: {t1 - t0:.2f}s")

    time.sleep(1.0)
    print("正在批量获取历史日K行情...")
    t0 = time.time()
    klines = client.klines.batch(symbols, period="1d", count=120, as_dataframe=True)
    t1 = time.time()
    print(f"日K获取完毕，耗时: {t1 - t0:.2f}s")

    # 预处理日K索引
    kline_data = {}
    for sym, df_k in klines.items():
        k = df_k.copy()
        k["trade_date"] = pd.to_datetime(k["trade_date"]).dt.strftime("%Y-%m-%d")
        kline_data[sym] = k.set_index("trade_date")

    print("\n开始逐日执行动态 ST 过滤、停牌剔除与微盘 20 计算...")
    daily_metrics_rows = []
    daily_details_rows = []

    for target_date in target_dates:
        day_active_stocks = []
        for sym in symbols:
            code = sym.split(".")[0]
            name, total_shares = inst_map.get(sym, ("", 0.0))

            # 1. ST 过滤 (若当天已正式生效 ST 则剔除)
            st_eff = ST_LEDGER.get(code)
            if st_eff and target_date >= st_eff:
                continue

            df_k = kline_data.get(sym)
            if df_k is None or target_date not in df_k.index:
                # 当天停牌无K线
                continue

            row = df_k.loc[target_date]
            close = float(row["close"]) if pd.notna(row.get("close")) else None
            vol = float(row["volume"]) if pd.notna(row.get("volume")) else 0.0
            amt = float(row["amount"]) if pd.notna(row.get("amount")) else 0.0

            # 2. 停牌过滤 (成交量为0或收盘价异常)
            if vol == 0 or close is None or close <= 0:
                continue

            if total_shares and close and total_shares > 0:
                mcap = round(close * total_shares / 1e8, 2)
            else:
                continue

            day_active_stocks.append({
                "代码": code,
                "名称": name,
                "最新价": round(close, 2),
                "成交量": int(vol),
                "成交额": round(amt, 2),
                "总市值(亿元)": mcap,
            })

        day_df = pd.DataFrame(day_active_stocks)
        day_df = day_df.sort_values("总市值(亿元)").reset_index(drop=True)

        # 最小 20 只
        top20 = day_df.head(20).copy()
        micro20_mean = round(float(top20["总市值(亿元)"].mean()), 2)

        # 第 200 名
        rank200_val = pd.NA
        rank200_stock = pd.NA
        if len(day_df) >= 200:
            r200 = day_df.iloc[199]
            rank200_val = r200["总市值(亿元)"]
            rank200_stock = f"{r200['代码']} {r200['名称']}"

        daily_metrics_rows.append({
            "日期": pd.Timestamp(target_date),
            "有效股票数": len(day_df),
            "第200名市值(亿元)": rank200_val,
            "第200名股票": rank200_stock,
            "微盘20均值(亿元)": micro20_mean,
            "口径": "历史重构扩展",
        })

        top20_str = "; ".join([f"{r['代码']} {r['名称']}({r['总市值(亿元)']:.2f}亿)" for _, r in top20.iterrows()])
        daily_details_rows.append({
            "日期": target_date,
            "有效股票数": len(day_df),
            "第200名市值(亿元)": rank200_val,
            "第200名股票": rank200_stock,
            "微盘20均值(亿元)": micro20_mean,
            "后20名标的清单": top20_str,
        })

    ext_metrics_df = pd.DataFrame(daily_metrics_rows)
    ext_details_df = pd.DataFrame(daily_details_rows)

    ext_metrics_df.to_csv(ext_file, index=False, encoding="utf-8-sig")
    ext_details_df.to_csv(details_file, index=False, encoding="utf-8-sig")
    print(f"\n✅ 历史扩展时序数据已成功持久化至:\n   {ext_file}\n   {details_file}")

    # 输出前后检验
    print("\n" + "=" * 75)
    print("📊 关键时间节点校验与衔接分析:")
    print("=" * 75)

    print("\n【4月底年报出清至5月初ST生效过渡】:")
    sub_may = ext_details_df[(ext_details_df["日期"] >= "2026-04-28") & (ext_details_df["日期"] <= "2026-05-08")]
    for _, r in sub_may.iterrows():
        print(f"  {r['日期']} | 有效标的: {r['有效股票数']} | 微盘20均值: {r['微盘20均值(亿元)']:.2f}亿 | 第200名: {r['第200名市值(亿元)']}亿 ({r['第200名股票']})")

    print("\n【6月中旬重构末日与6月22日实测快照首日衔接】:")
    sub_june = ext_details_df[ext_details_df["日期"] >= "2026-06-15"]
    for _, r in sub_june.iterrows():
        print(f"  {r['日期']} | 有效标的: {r['有效股票数']} | 微盘20均值: {r['微盘20均值(亿元)']:.2f}亿 | 第200名: {r['第200名市值(亿元)']}亿")

    from services.microcap import build_microcap_snapshot_metrics
    snap_first = build_microcap_snapshot_metrics(df_snap[df_snap["快照日期"] == "2026-06-22"]).iloc[0]
    print(f"  2026-06-22 [实测首日] | 有效标的: {snap_first['有效股票数']} | 微盘20均值: {snap_first['微盘20均值(亿元)']:.2f}亿 | 第200名: {snap_first['第200名市值(亿元)']}亿")

    return ext_metrics_df

if __name__ == "__main__":
    run_build_history_extension()
