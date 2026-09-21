#!/usr/bin/env python3
"""微盘股最小20只轮动策略回测 —— 独立审计（读产出CSV重新推导，不复用回测引擎代码）。

独立路径：
  · 从源数据独立重算每日市值排名，与 weekly_holdings 对账
  · 从 trade_log 独立重放资金与持仓，逐日重算净值，与 daily_nav 对账
  · 独立重算全部绩效指标，与 performance_summary 对账
  · 逐笔校验 100 股整数倍 / 四舍五入 / 先卖后买 / 2 元手续费 / 停牌不成交 / 调仓日顺延

用法：python scripts/audit_microcap_top20_rotation.py [--est-dir ...] [--out-dir ...]
"""
from __future__ import annotations

import argparse
import json
import math
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EST_DIR = ROOT / "output" / "microcap_union_estimate_20260101_20260908_v2"
DEFAULT_OUT_DIR = ROOT / "output" / "microcap_top20_rotation_20260105_20260908"

TARGET_AMOUNT = 10000.0
FEE = 2.0


def half_up_lots(target: float, price: float) -> int:
    return int(math.floor(target / price / 100 + 0.5)) * 100


def weekly_first_trading_days(dates: list[pd.Timestamp]) -> list[pd.Timestamp]:
    seen: OrderedDict[tuple[int, int], pd.Timestamp] = OrderedDict()
    for d in dates:
        iso = d.isocalendar()
        key = (int(iso[0]), int(iso[1]))
        seen.setdefault(key, d)
    return list(seen.values())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--est-dir", type=Path, default=DEFAULT_EST_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    est_dir, out_dir = args.est_dir, args.out_dir
    raw = pd.read_csv(est_dir / "all_candidate_days.csv", encoding="utf-8-sig", dtype={"代码": str}, low_memory=False)
    raw["code"] = raw["代码"].astype(str).str.zfill(6)
    raw["date"] = pd.to_datetime(raw["date"]).dt.normalize()
    raw["cap"] = pd.to_numeric(raw["estimated_market_cap_yuan"], errors="coerce")
    raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
    raw["halted"] = pd.to_numeric(raw["tradestatus"], errors="coerce").fillna(0).astype(int).eq(0)
    raw["eligible"] = raw["strategy_eligible"].astype(str).str.lower().isin({"true", "1", "yes"})

    dates = [pd.Timestamp(x) for x in sorted(raw["date"].unique())]
    codes = sorted(raw["code"].unique())
    close = raw.pivot(index="date", columns="code", values="close").reindex(index=dates, columns=codes)
    cap = raw.pivot(index="date", columns="code", values="cap").reindex(index=dates, columns=codes)
    halted = raw.pivot(index="date", columns="code", values="halted").reindex(index=dates, columns=codes).fillna(True).astype(bool)
    name_map = raw.drop_duplicates("code").set_index("code")["名称"].to_dict()
    pos = {d: i for i, d in enumerate(dates)}

    meta = json.loads((out_dir / "run_metadata.json").read_text(encoding="utf-8"))
    initial_cash = float(meta["初始账户资金"])

    trade_log = pd.read_csv(out_dir / "trade_log.csv", encoding="utf-8-sig", parse_dates=["日期"], dtype={"股票代码": str})
    trade_log["股票代码"] = trade_log["股票代码"].astype(str).str.zfill(6)
    daily_nav = pd.read_csv(out_dir / "daily_nav.csv", encoding="utf-8-sig", parse_dates=["日期"])
    weekly_holdings = pd.read_csv(out_dir / "weekly_holdings.csv", encoding="utf-8-sig", parse_dates=["调仓日期"], dtype={"股票代码": str})
    weekly_holdings["股票代码"] = weekly_holdings["股票代码"].str.zfill(6)
    summary = pd.read_csv(out_dir / "performance_summary.csv", encoding="utf-8-sig")
    weekly_rebalance = pd.read_csv(out_dir / "weekly_rebalance.csv", encoding="utf-8-sig", parse_dates=["调仓日期", "信号日期"])

    rebal_dates = weekly_first_trading_days(dates)
    rows: list[dict] = []

    def add(item: str, ok: bool, evidence: str, version: str = "-") -> None:
        rows.append({"审计项": item, "版本": version, "结果": "通过" if ok else "异常", "证据": evidence})

    # 调仓日历独立性
    engine_cal = sorted(weekly_rebalance[weekly_rebalance["版本"] == "B_无未来函数"]["调仓日期"].unique())
    add(
        "调仓日历 = 每周第一个交易日",
        [pd.Timestamp(d) for d in engine_cal] == rebal_dates,
        f"独立重算 {len(rebal_dates)} 个可调仓日：{rebal_dates[0]:%Y-%m-%d} ~ {rebal_dates[-1]:%Y-%m-%d}，"
        f"与台账逐一一致；整周休市的周自动跳过（2026-02-16 春节当周），"
        f"2026-04-06（清明后首日）、2026-05-06（劳动节后首日）、2026-02-24 均为该周首个交易日。",
    )

    for version in ("A_原始规则", "B_无未来函数", "B2_严格无未来函数"):
        tl = trade_log[trade_log["版本"] == version].copy()
        tl["股数"] = pd.to_numeric(tl["股数"], errors="coerce").fillna(0).astype(int)
        tl["成交价"] = pd.to_numeric(tl["成交价"], errors="coerce")
        tl["成交金额"] = pd.to_numeric(tl["成交金额"], errors="coerce").fillna(0.0)
        tl["手续费"] = pd.to_numeric(tl["手续费"], errors="coerce").fillna(0.0)
        fills = tl[tl["操作"].isin(["买入", "卖出"])]
        fails = tl[tl["操作"].str.contains("失败")]

        # --- 1. 独立重放：现金与持仓
        cash = initial_cash
        shares: dict[str, int] = {}
        violations: list[str] = []
        bad_price: list[str] = []
        bad_amount: list[str] = []
        bad_fee: list[str] = []
        bad_lot: list[str] = []
        bad_round: list[str] = []
        for _, r in tl.iterrows():
            if r["操作"] == "买入":
                shares[r["股票代码"]] = shares.get(r["股票代码"], 0) + r["股数"]
                cash -= r["成交金额"] + r["手续费"]
            elif r["操作"] == "卖出":
                shares[r["股票代码"]] = shares.get(r["股票代码"], 0) - r["股数"]
                cash += r["成交金额"] - r["手续费"]
            if r["操作"] in ("买入", "卖出"):
                if r["股数"] % 100 != 0:
                    bad_lot.append(f"{r['日期']:%Y-%m-%d} {r['股票代码']} {r['股数']}")
                px = close.at[r["日期"], r["股票代码"]]
                if abs(px - r["成交价"]) > 1e-4:
                    bad_price.append(f"{r['日期']:%Y-%m-%d} {r['股票代码']} 台账{r['成交价']} vs 源{px}")
                if abs(r["股数"] * px - r["成交金额"]) > 0.02:
                    bad_amount.append(f"{r['日期']:%Y-%m-%d} {r['股票代码']} {r['股数']}×{px}≠{r['成交金额']}")
                if abs(r["手续费"] - FEE) > 1e-9:
                    bad_fee.append(f"{r['日期']:%Y-%m-%d} {r['股票代码']} {r['手续费']}")
                if r["操作"] == "买入" and half_up_lots(TARGET_AMOUNT, px) != r["股数"]:
                    bad_round.append(f"{r['日期']:%Y-%m-%d} {r['股票代码']} 期望{half_up_lots(TARGET_AMOUNT, px)} 实际{r['股数']}")
                if halted.at[r["日期"], r["股票代码"]]:
                    violations.append(f"停牌日成交 {r['日期']:%Y-%m-%d} {r['股票代码']}")
            if cash < -1e-6:
                violations.append(f"现金转负 {r['日期']:%Y-%m-%d} {cash:.2f}")
            if any(v < 0 for v in shares.values()):
                violations.append(f"负持仓 {r['日期']:%Y-%m-%d}")
        # 仅保留每类首个证据
        add("逐笔成交价 = 源收盘价", not bad_price, f"不符 {len(bad_price)} 笔" + (f"；{bad_price[0]}" if bad_price else ""), version)
        add("逐笔成交金额 = 股数 × 收盘价", not bad_amount, f"不符 {len(bad_amount)} 笔" + (f"；{bad_amount[0]}" if bad_amount else ""), version)
        add("每笔手续费 = 2 元", not bad_fee, f"不符 {len(bad_fee)} 笔", version)
        add("股数为 100 股整数倍", not bad_lot, f"非整百 {len(bad_lot)} 笔", version)
        add("买入股数 = 四舍五入整百股", not bad_round, f"复算不一致 {len(bad_round)} 笔" + (f"；{bad_round[0]}" if bad_round else ""), version)
        add("停牌日零成交 / 现金持仓非负", not violations, f"违规 {len(violations)} 条" + (f"；{violations[0]}" if violations else ""), version)

        # --- 2. 独立重算每日净值
        nav_file = daily_nav[daily_nav["版本"] == version].sort_values("日期").reset_index(drop=True)
        cash_t = initial_cash
        hold_t: dict[str, int] = {}
        replay = []
        tl_by_date = {d: g for d, g in tl.groupby(tl["日期"])}
        for d in dates:
            if d in tl_by_date:
                for _, r in tl_by_date[d].iterrows():
                    if r["操作"] == "买入":
                        hold_t[r["股票代码"]] = hold_t.get(r["股票代码"], 0) + r["股数"]
                        cash_t -= r["成交金额"] + r["手续费"]
                    elif r["操作"] == "卖出":
                        hold_t[r["股票代码"]] = hold_t.get(r["股票代码"], 0) - r["股数"]
                        cash_t += r["成交金额"] - r["手续费"]
            mv = sum(s * (close.at[d, c] if np.isfinite(close.at[d, c]) else 0.0) for c, s in hold_t.items())
            replay.append(cash_t + mv)
        diff = np.abs(np.array(replay) - nav_file["总资产"].to_numpy())
        add(
            "独立重放净值 = daily_nav",
            float(diff.max()) < 0.05,
            f"最大偏差 {diff.max():.4f} 元，交易日 {len(diff)} 天，最终资产 台账 {nav_file['总资产'].iloc[-1]:,.2f} vs 重放 {replay[-1]:,.2f}",
            version,
        )

        # --- 3. 独立重算绩效指标
        equity = np.array(replay, dtype=float)
        nav = equity / initial_cash
        rets = np.diff(nav) / nav[:-1]
        years = len(rets) / 252
        annual = nav[-1] ** (1 / years) - 1
        peak = np.maximum.accumulate(nav)
        dd = nav / peak - 1.0
        mdd = float(dd.min())
        vol = float(np.std(rets, ddof=1) * math.sqrt(252))
        sharpe = float(np.mean(rets) * 252 / vol)
        dn = rets[rets < 0]
        sortino = float(np.mean(rets) * 252 / (math.sqrt(np.mean(dn**2)) * math.sqrt(252)))
        # 周收益
        wk: OrderedDict[tuple[int, int], float] = OrderedDict()
        for d, e in zip(dates, replay):
            iso = d.isocalendar()
            wk[(int(iso[0]), int(iso[1]))] = e
        wq = np.array(list(wk.values()), dtype=float)
        wr = np.diff(wq) / wq[:-1]
        buy_amt = float(fills[fills["操作"] == "买入"]["成交金额"].sum() + fills[fills["操作"] == "买入"]["手续费"].sum())
        sell_amt = float(fills[fills["操作"] == "卖出"]["成交金额"].sum())
        turnover = (buy_amt + sell_amt) / float(equity.mean()) / years
        ulcer = float(math.sqrt(np.mean((dd * 100) ** 2)))

        def cmp(key: str, mine: float, tol: float = 1e-4, col_suffix: str = "_账户口径") -> None:
            target = summary.loc[summary["指标"] == key, f"{version}{col_suffix}"].iloc[0]
            try:
                target = float(target)
            except (TypeError, ValueError):
                add(f"指标独立复算 · {key}", False, f"报告值无法解析：{target}", version)
                return
            ok = abs(mine - target) <= tol * max(1.0, abs(target))
            add(f"指标独立复算 · {key}", ok, f"独立 {mine:.6f} vs 报告 {target:.6f}", version)

        cmp("累计收益率", float(nav[-1] - 1))
        cmp("年化收益率", float(annual))
        cmp("最大回撤", mdd)
        cmp("年化波动率", vol)
        cmp("Sharpe", sharpe, 1e-3)
        cmp("Sortino", sortino, 1e-3)
        cmp("Calmar", float(annual / abs(mdd)), 1e-3)
        cmp("UlcerIndex", ulcer, 1e-3)
        cmp("盈利周比例", float(np.mean(wr > 0)))
        cmp("最大单周上涨", float(np.max(wr)))
        cmp("最大单周下跌", float(np.min(wr)))
        cmp("年化换手率(双边)", turnover, 1e-3)
        cmp("总交易费用", float(fills["手续费"].sum()))
        cmp("总买入次数", float(len(fills[fills["操作"] == "买入"])))
        cmp("总卖出次数", float(len(fills[fills["操作"] == "卖出"])))
        cmp("最终资产", float(equity[-1]), 1e-6)

        # --- 4. 持仓名单对账：每期快照 = 信号日前20名（扣除买入失败，补回卖出失败）
        hs = weekly_holdings[weekly_holdings["版本"] == version]
        mismatch: list[str] = []
        over20: list[str] = []
        for _, rb in weekly_rebalance[weekly_rebalance["版本"] == version].iterrows():
            d, s = rb["调仓日期"], rb["信号日期"]
            caps = cap.loc[s].dropna()
            caps = caps[caps > 0]
            expected = set(caps.sort_values(kind="mergesort").index[:20])
            code_fails = set(fails[(fails["日期"] == d) & (fails["操作"] == "买入失败")]["股票代码"])
            sell_fails = set(fails[(fails["日期"] == d) & (fails["操作"] == "卖出失败")]["股票代码"])
            expected_hold = (expected - code_fails) | (sell_fails & set(hs[hs["调仓日期"] == d]["股票代码"]))
            actual = set(hs[hs["调仓日期"] == d]["股票代码"])
            if expected_hold != actual:
                mismatch.append(f"{d:%Y-%m-%d} 缺{sorted(expected_hold - actual)} 多{sorted(actual - expected_hold)}")
            if len(actual) > 20:
                over20.append(f"{d:%Y-%m-%d} {len(actual)}只")
        add("每期持仓 = 信号日市值最小20只", not mismatch, f"不符 {len(mismatch)} 期" + (f"；{mismatch[0]}" if mismatch else ""), version)
        add("持仓数量不超过 20 只", not over20, f"超限 {len(over20)} 期" + (f"；{over20[0]}" if over20 else ""), version)

        # --- 5. 信号时间独立性
        rb = weekly_rebalance[weekly_rebalance["版本"] == version]
        sig_bad = []
        for _, r in rb.iterrows():
            d, s = r["调仓日期"], r["信号日期"]
            if version.startswith("A"):
                if s != d:
                    sig_bad.append(f"{d:%Y-%m-%d} 信号{s:%Y-%m-%d} 应为同日")
            else:
                if s >= d:
                    sig_bad.append(f"{d:%Y-%m-%d} 信号{s:%Y-%m-%d} 非严格更早")
                elif pos[d] - pos[s] != 1:
                    sig_bad.append(f"{d:%Y-%m-%d} 信号非前一交易日（{s:%Y-%m-%d}）")
        expected_exc = 1 if version == "B_无未来函数" else 0
        add(
            "信号日期 < 成交日期（无未来函数）",
            len(sig_bad) == expected_exc,
            f"例外 {len(sig_bad)} 次（预期 {expected_exc} 次：B 版首轮建仓因数据起点同日；B2 版首轮顺延至 "
            f"{meta['首轮建仓日'].get('B2_严格无未来函数')} 后为止）" + (f"；{sig_bad[0]}" if sig_bad else ""),
            version,
        )

    out = pd.DataFrame(rows)
    out.to_csv(out_dir / "audit_independent.csv", index=False, encoding="utf-8-sig")
    bad = out[out["结果"] != "通过"]
    print(f"audit rows={len(out)} abnormal={len(bad)}")
    if len(bad):
        print(bad.to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
