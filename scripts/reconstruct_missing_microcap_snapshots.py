#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reconstruct missing microcap constituent snapshots for BK1158 using TickFlow.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
import pandas as pd
import numpy as np

# Ensure dashboard root is in python path
ROOT = Path("/home/renne/investment_dashboard")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tickflow import TickFlow
from core.cache import save_dataset, load_dataset
from services.microcap import (
    CONSTITUENT_SNAPSHOT_SYMBOL,
    CONSTITUENT_SNAPSHOT_SOURCE,
    CONSTITUENT_SNAPSHOT_DATA_TYPE,
    normalize_microcap_constituent_snapshots,
    build_microcap_snapshot_metrics,
    load_microcap_constituent_snapshots,
)

MISSING_DATES = [
    "2026-06-24",
    "2026-07-03",
    "2026-08-10",
    "2026-08-12",
    "2026-08-18",
    "2026-08-21",
]

def to_tickflow_symbol(code: str) -> str:
    c = str(code).strip().zfill(6)
    return f"{c}.SH" if c.startswith(("6", "9")) else f"{c}.SZ"

def run_reconstruction(dry_run: bool = True, force_fetch: bool = False):
    raise RuntimeError(
        "旧重建已停用：未来成分并集和当前股本不满足PIT，禁止生成或合入正式快照。"
        "请使用 scripts/audit_microcap_research_inputs.py 审计既有数据。"
    )

    # Retained temporarily for audit of existing legacy artifacts; unreachable.
    out_dir = ROOT / "data" / "raw" / "eastmoney" / "reconstructed"
    out_dir.mkdir(parents=True, exist_ok=True)
    recon_file = out_dir / "microcap_bk1158_snapshots_reconstructed_6days.csv"
    summary_file = out_dir / "microcap_reconstructed_metrics_summary.csv"

    print("=" * 70)
    print("🚀 微盘股（BK1158）缺失交易日真实成分快照重构与合入工程")
    print(f"模式: {'【Dry Run 仅生成核查文件，不合入主快照】' if dry_run else '【正式合入主快照】'}")
    print(f"目标缺失交易日: {', '.join(MISSING_DATES)}")
    print("=" * 70)

    # 1. 检查是否存在已生成的重构文件
    recon_df = None
    if recon_file.exists() and not force_fetch:
        try:
            cached_recon = pd.read_csv(recon_file)
            cached_dates = set(cached_recon["快照日期"].astype(str).unique())
            if set(MISSING_DATES).issubset(cached_dates) and len(cached_recon) == len(MISSING_DATES) * 400:
                print(f"ℹ️ 发现已生成的 6 日完整重构文件: {recon_file} (2400行)")
                recon_df = cached_recon
                recon_df["快照时间"] = recon_df["快照日期"].astype(str) + " 15:00:00"
                recon_df["代码"] = recon_df["代码"].astype(str).str.zfill(6)
                recon_df = normalize_microcap_constituent_snapshots(recon_df)
        except Exception as e:
            print(f"⚠️ 读取现有重构文件失败: {e}，将重新拉取。")

    if recon_df is None:
        # 加载当前已有快照
        csv_path = ROOT / "data" / "raw" / "eastmoney" / "microcap_bk1158_constituent_snapshots_1d.csv"
        existing_df = pd.read_csv(csv_path)
        d_col = [c for c in existing_df.columns if "date" in c.lower() or "日期" in c][0]
        existing_df[d_col] = pd.to_datetime(existing_df[d_col]).dt.strftime("%Y-%m-%d")

        # 获取完整 A 股交易日历
        df_300 = pd.read_csv(ROOT / "data" / "raw" / "index_history" / "index_raw_000300.SH_1d.csv")
        d_300 = [c for c in df_300.columns if "date" in c.lower() or "日期" in c][0]
        all_trading_days = sorted(pd.to_datetime(df_300[d_300]).dt.strftime("%Y-%m-%d").unique())

        # 初始化 TickFlow
        client = TickFlow.free()

        all_reconstructed_rows = []
        summary_records = []

        for target_date in MISSING_DATES:
            idx = all_trading_days.index(target_date)
            prev_date = all_trading_days[idx - 1]
            next_date = all_trading_days[idx + 1]

            prev_stocks = existing_df[existing_df[d_col] == prev_date]["代码"].astype(str).str.zfill(6).tolist()
            next_stocks = existing_df[existing_df[d_col] == next_date]["代码"].astype(str).str.zfill(6).tolist()
            candidate_codes = sorted(set(prev_stocks + next_stocks))
            candidate_symbols = [to_tickflow_symbol(c) for c in candidate_codes]

            print(f"\n▶ 正在重构 [{target_date}] (前一日: {prev_date}, 后一日: {next_date})")
            print(f"  候选池标的数 (两日并集): {len(candidate_codes)} 只")

            # 带速率控制与重试的获取
            inst_map = {}
            for attempt in range(5):
                try:
                    t0 = time.time()
                    inst_list = client.instruments.batch(candidate_symbols)
                    inst_map = {
                        r["symbol"]: (r.get("name", ""), r.get("ext", {}).get("total_shares", 0.0))
                        for r in inst_list
                    }
                    t1 = time.time()
                    print(f"  标的信息获取完成，耗时: {t1 - t0:.2f}s")
                    break
                except Exception as exc:
                    print(f"  [重试 {attempt+1}] 标的信息获取异常: {exc}，等待 12s...")
                    time.sleep(12)

            time.sleep(1.5)  # 避免瞬时超频

            klines = {}
            for attempt in range(5):
                try:
                    t0 = time.time()
                    klines = client.klines.batch(candidate_symbols, period="1d", count=60, as_dataframe=True)
                    t1 = time.time()
                    print(f"  日K线获取完成，耗时: {t1 - t0:.2f}s")
                    break
                except Exception as exc:
                    print(f"  [重试 {attempt+1}] 日K线获取异常: {exc}，等待 12s...")
                    time.sleep(12)

            time.sleep(1.5)

            day_candidates = []
            for sym, k_df in klines.items():
                code = sym.split(".")[0]
                name, total_shares = inst_map.get(sym, ("", 0.0))
                k_df = k_df.copy()
                k_df["trade_date"] = pd.to_datetime(k_df["trade_date"]).dt.strftime("%Y-%m-%d")
                sub = k_df[k_df["trade_date"] == target_date]
                if sub.empty:
                    continue
                row = sub.iloc[0]
                close = float(row["close"]) if pd.notna(row.get("close")) else None
                vol = float(row["volume"]) if pd.notna(row.get("volume")) else 0.0
                amt = float(row["amount"]) if pd.notna(row.get("amount")) else 0.0

                sub_idx = k_df.index[k_df["trade_date"] == target_date].tolist()[0]
                change_pct = None
                if sub_idx > 0:
                    prev_close = float(k_df.iloc[sub_idx - 1]["close"])
                    if prev_close > 0 and close:
                        change_pct = round(((close - prev_close) / prev_close) * 100, 2)

                is_suspended = (vol == 0 or close is None or close <= 0)
                if is_suspended:
                    continue

                if total_shares and close and total_shares > 0:
                    mcap = round(close * total_shares / 1e8, 2)
                else:
                    mcap = None

                if mcap and mcap > 0:
                    day_candidates.append({
                        "快照日期": target_date,
                        "快照时间": f"{target_date} 15:00:00",
                        "代码": code,
                        "名称": name,
                        "最新价": round(close, 2),
                        "涨跌幅(%)": change_pct if change_pct is not None else np.nan,
                        "成交量": int(vol),
                        "成交额": round(amt, 2),
                        "总市值(亿元)": mcap,
                        "是否停牌": False,
                    })

            day_df = pd.DataFrame(day_candidates)
            day_df = day_df.sort_values("总市值(亿元)").reset_index(drop=True)
            day_df_400 = day_df.head(400).copy()
            day_df_400["排名"] = range(1, len(day_df_400) + 1)

            cols = [
                "快照日期", "快照时间", "排名", "代码", "名称",
                "最新价", "涨跌幅(%)", "成交量", "成交额", "总市值(亿元)", "是否停牌"
            ]
            day_df_400 = day_df_400[cols]
            all_reconstructed_rows.append(day_df_400)

            micro20_mean = round(float(day_df_400.head(20)["总市值(亿元)"].mean()), 2)
            rank200_row = day_df_400.iloc[199]
            summary_records.append({
                "日期": target_date,
                "前一日(T-1)": prev_date,
                "后一日(T+1)": next_date,
                "候选池标的数": len(candidate_codes),
                "可交易标的数": len(day_df),
                "截取保存标的数": len(day_df_400),
                "微盘20均值(亿元)": micro20_mean,
                "第200名股票": f"{rank200_row['代码']} {rank200_row['名称']}",
                "第200名市值(亿元)": rank200_row["总市值(亿元)"],
            })

        recon_df = pd.concat(all_reconstructed_rows, ignore_index=True)
        summary_df = pd.DataFrame(summary_records)
        recon_df.to_csv(recon_file, index=False, encoding="utf-8-sig")
        summary_df.to_csv(summary_file, index=False, encoding="utf-8-sig")
        print(f"\n✅ 独立重构快照已更新保存至: {recon_file} (总行数: {len(recon_df)})")

    # 2. 输出前后交易日指标衔接与平滑度报告
    print("\n" + "=" * 70)
    print("📊 6 个缺失交易日指标与前后交易日衔接对比表:")
    print("=" * 70)

    existing_all, _ = load_microcap_constituent_snapshots()
    full_preview = pd.concat([existing_all, recon_df], ignore_index=True)
    full_metrics = build_microcap_snapshot_metrics(full_preview)
    full_metrics["日期_str"] = pd.to_datetime(full_metrics["日期"]).dt.strftime("%Y-%m-%d")

    summary_df = pd.read_csv(summary_file)
    for _, row in summary_df.iterrows():
        t = row["日期"]
        t_prev = row["前一日(T-1)"]
        t_next = row["后一日(T+1)"]
        sub_m = full_metrics[full_metrics["日期_str"].isin([t_prev, t, t_next])].copy()
        print(f"\n【{t} 窗口衔接】:")
        print(sub_m[["日期_str", "有效股票数", "第200名市值(亿元)", "第200名股票", "微盘20均值(亿元)"]].to_string(index=False))

    # 3. 如果为 commit 模式，执行原子合入
    if not dry_run:
        print("\n" + "=" * 70)
        print("⚡ 执行正式合并入系统主快照与缓存...")
        cleaned_existing = existing_all[~existing_all["快照日期"].isin(MISSING_DATES)].copy()
        recon_df["快照时间"] = recon_df["快照日期"].astype(str) + " 15:00:00"
        recon_df["代码"] = recon_df["代码"].astype(str).str.zfill(6)
        norm_recon = normalize_microcap_constituent_snapshots(recon_df)

        merged_all = pd.concat([cleaned_existing, norm_recon], ignore_index=True)
        merged_all = normalize_microcap_constituent_snapshots(merged_all)

        save_dataset(
            symbol=CONSTITUENT_SNAPSHOT_SYMBOL,
            name="BK1158 微盘股每日成分快照",
            source=CONSTITUENT_SNAPSHOT_SOURCE,
            data_type=CONSTITUENT_SNAPSHOT_DATA_TYPE,
            df=merged_all,
        )
        print(f"🎉 成功完成合并！主快照当前状态:")
        print(f"   总记录行数: {len(merged_all)} 行")
        print(f"   总收录交易日数: {len(merged_all['快照日期'].unique())} 天 (完整覆盖 2026-06-22 至 2026-09-04)")
        print(f"   缺失交易日数: 0 天 (100% 完整率)")

    return recon_df

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="正式合入主快照")
    parser.add_argument("--force-fetch", action="store_true", help="强制重新拉取 TickFlow 数据")
    args = parser.parse_args()
    run_reconstruction(dry_run=not args.commit, force_fetch=args.force_fetch)
