"""Read-only, hash-bound import of the explicit handover inputs."""
import hashlib
import io
import json
import re
from pathlib import Path
import numpy as np
import pandas as pd
from services.microcap_rotation_metrics import metrics, display_metrics, LABELS

CALC_VERSION = "abcd-research-1"
INPUTS = ["01_核心对比与审计总表/" + n for n in
          ["daily_nav_abcd.csv", "comparison_summary_abcd.csv", "ma_timing_log.csv", "timing_period_audit_cd.csv"]]
INPUTS += [f"02_各策略逐笔交易明细/trade_log_{s}.csv" for s in "bcd"]
INPUTS += [f"03_历史持仓与周度调仓记录/weekly_holdings_{s}.csv" for s in "bcd"]
INPUTS += ["00_交接指引与审计备忘/" + n for n in
           ["AUDIT_HANDOVER_README.md", "BACKTEST_REPORT.md", "v2.2修订对照与闭环说明.md"]]
DISCLOSURE = "后期候选池与固定股本估算，非严格历史时点回测；历史研究与新模拟独立。"

def manifest(root):
    root = Path(root)
    return {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in INPUTS}

def import_research(root, expected=None, strict_summary=False):
    root = Path(root)
    blobs = {name: (root/name).read_bytes() for name in INPUTS}
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in blobs.items()}
    if expected is not None and (expected.get("calculation_version") != CALC_VERSION or expected.get("hashes") != hashes):
        raise ValueError("历史输入哈希或计算版本不匹配；拒绝使用旧核验结果")
    def read(name):
        raw = next(raw for path, raw in blobs.items() if Path(path).name == name)
        return pd.read_csv(io.BytesIO(raw), dtype={"股票代码": str})
    nav = read("daily_nav_abcd.csv")
    if nav.empty or nav.date.duplicated().any() or not nav.date.is_monotonic_increasing:
        raise ValueError("历史净值日期为空、重复或乱序")
    transforms, computed, trades, holdings, normalized = [], {}, {}, {}, []
    for s in "ABCD":
        eq = pd.to_numeric(nav[f"{s}_total_equity"], errors="raise")
        if not np.isfinite(eq).all() or (eq <= 0).any():
            raise ValueError("历史权益含无效值")
        cash = nav[f"{s}_cash"] if s != "A" else pd.Series(0., index=nav.index)
        f = pd.DataFrame(dict(date=nav.date, equity=eq, cash=cash))
        fills_count = fee = 0
        if s != "A":
            t = read(f"trade_log_{s.lower()}.csv")
            h = read(f"weekly_holdings_{s.lower()}.csv").rename(columns={"调仓日期":"日期","持股数量":"股数"})
            balance, position, daily_cash, position_days = 220000., {}, {}, {}
            for _, tr in t.iterrows():
                if tr["操作"] in ("买入", "卖出"):
                    code = tr["股票代码"].zfill(6)
                    qty, amount = float(tr["股数"]), float(tr["成交金额"])
                    sign = 1 if tr["操作"] == "买入" else -1
                    if qty <= 0 or qty != int(qty) or abs(amount-qty*float(tr["成交价"])) > .011 or float(tr["手续费"]) != 2:
                        raise ValueError(f"{s} 成交金额、数量或佣金不符")
                    balance += -sign*amount-float(tr["手续费"])
                    position[code] = position.get(code, 0) + sign*qty
                    if balance < -.001 or min(position.values()) < 0 or abs(balance-float(tr["变动后可用现金"])) > .011:
                        raise ValueError(f"{s} 逐笔资金或持仓不闭合")
                    fills_count += 1
                    fee += float(tr["手续费"])
                daily_cash[tr["日期"]] = balance
                position_days[tr["日期"]] = position.copy()
            balance = 220000.
            for _, row in nav.iterrows():
                balance = daily_cash.get(row.date, balance)
                if abs(balance-row[f"{s}_cash"]) > .011:
                    raise ValueError(f"{s} {row.date} 日末现金不闭合")
                values = row[f"{s}_cash"]+row[f"{s}_stock_value"]+(row.D_etf_value if s=="D" else 0)
                if abs(values-row[f"{s}_total_equity"]) > .011:
                    raise ValueError(f"{s} 日结资产不闭合")
            if h.duplicated(["日期","股票代码"]).any():
                raise ValueError("历史持仓重复")
            for raw_day, group in h.groupby("日期"):
                day = str(raw_day).removesuffix("(期末快照)")
                if abs(group["持仓市值"]-group["股数"]*group["当日收盘价"]).max() > .011:
                    raise ValueError(f"{s} 持仓数量乘价格不符")
                available = [d for d in position_days if d <= day]
                ledger_positions = position_days[max(available)] if available else {}
                observed = {str(r["股票代码"]).zfill(6):float(r["股数"]) for _,r in group.iterrows()}
                expected_positions = {c:q for c,q in ledger_positions.items() if q > 0}
                if observed != expected_positions:
                    raise ValueError(f"{s} {day} 持仓数量与逐笔成交不符")
                daily = nav.loc[nav.date == day]
                if len(daily)!=1 or abs(group["持仓市值"].sum()-float(daily[f"{s}_stock_value"].iloc[0])-(float(daily.D_etf_value.iloc[0]) if s=="D" else 0)) > .011:
                    raise ValueError(f"{s} 持仓汇总不符")
            trades[s], holdings[s] = t.to_dict("records"), h.to_dict("records")
        # Explicit per-column scale; never guess scale from today's magnitude.
        for asset in ("cash", "stock", "etf"):
            col = f"{s}_{asset}_pos_pct"
            value_col = f"{s}_{asset}_value" if asset != "cash" else f"{s}_cash"
            if col in nav:
                scale = 100. if s == "D" else 1.
                ratio = nav[col]/scale
                actual = nav[value_col]/eq
                if ((ratio < 0)|(ratio > 1)).any() or abs(ratio-actual).max() > .00011:
                    raise ValueError(f"{col} 仓位单位或比例错误")
                transforms.append(dict(column=col, divide_by=scale, meaning="内部0—1；展示乘100；从金额重算"))
        days = int((nav.C_stock_value == 0).sum()) if s=="C" else int((nav.D_etf_shares > 0).sum()) if s=="D" else 0
        computed[s] = metrics(f, fills=fills_count, fees=fee, defensive_days=days)
        for row in f.to_dict("records"):
            normalized.append(dict(strategy=s, **row))
    source_summary = read("comparison_summary_abcd.csv")
    if len(source_summary) != 18 or len(source_summary.columns) < 6:
        raise ValueError("历史汇总表必须包含18项及4策略")
    comparison = pd.DataFrame({"指标": LABELS, **{s:display_metrics(computed[s]) for s in "ABCD"}})
    differences = []
    for j,s in enumerate("ABCD"):
        for i in range(18):
            old, new = str(source_summary.iloc[i,j+2]), str(comparison.iloc[i,j+1])
            if i in (5,6):
                equal = old == new
            else:
                pattern = r"[-+]?\d+(?:\.\d+)?"
                a = [float(v) for v in re.findall(pattern, old.replace(",","").replace("512890",""))]
                b = [float(v) for v in re.findall(pattern, new.replace(",",""))]
                equal = len(a)==len(b) and all(abs(x-y)<.011 for x,y in zip(a,b))
            if not equal:
                differences.append(dict(strategy=s, metric=LABELS[i], original=old, recomputed=new))
    unexpected = [d for d in differences if not (
        d["strategy"]=="C" and d["metric"]=="平均现金比例" and
        d["original"]=="0.55%" and d["recomputed"]=="55.22%")]
    if unexpected:
        raise ValueError(f"汇总表发现非已知差异：{unexpected}")
    if strict_summary and differences:
        raise ValueError(f"历史汇总存在{len(differences)}处差异")
    return dict(calculation_version=CALC_VERSION, hashes=hashes, disclosure=DISCLOSURE,
                metrics=computed, comparison=comparison.to_dict("records"), differences=differences,
                transforms=transforms, raw_nav=nav.to_dict("records"), daily=normalized,
                trades=trades, holdings=holdings, periods=read("timing_period_audit_cd.csv").to_dict("records"),
                source_summary=source_summary.to_dict("records"),
                documents={Path(p).name:raw.decode("utf-8-sig") for p,raw in blobs.items() if p.endswith(".md")})
