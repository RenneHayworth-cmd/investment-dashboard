"""Metrics are derived from unrounded equity, with initial capital seeded."""
import numpy as np
import pandas as pd

LABELS = ["期末总资产", "累计净利润", "累计收益率", "年化复合收益率",
          "最大历史回撤", "最大回撤峰值日期", "最大回撤谷底日期", "期末回撤",
          "最长水下交易日", "年化波动率", "夏普比率", "索提诺比率",
          "卡玛比率", "痛苦指数", "实际成交笔数及手续费", "平均现金比例",
          "避险配置天数及占比", "日度胜率 / 周度胜率"]

def metrics(frame, initial=220000., fills=0, fees=0., defensive_days=0, seed_row=False):
    f = frame.sort_values("date").reset_index(drop=True)
    e = f.equity.astype(float)
    r = e.pct_change().fillna(e.iloc[0] / initial - 1)
    peak = np.maximum.accumulate(np.r_[initial, e])[1:]
    dd = e / peak - 1
    trough = int(dd.idxmin())
    pk = int(e.iloc[:trough + 1].idxmax())
    streak = longest = 0
    for v in dd:
        streak = streak + 1 if v < -1e-10 else 0
        longest = max(longest, streak)
    observations = r.iloc[1:] if seed_row else r
    n = len(observations)
    cagr = (e.iloc[-1] / initial) ** (252 / n) - 1 if n else 0.
    vol = observations.std(ddof=1)
    ex = observations - .02 / 252
    downside = np.sqrt(np.mean(np.minimum(ex, 0) ** 2)) if n else 0.
    weekly = pd.Series(e.values, index=pd.to_datetime(f.date)).resample("W-FRI").last().dropna().pct_change().dropna()
    return dict(end_equity=float(e.iloc[-1]), profit=float(e.iloc[-1]-initial),
                return_pct=float((e.iloc[-1]/initial-1)*100), cagr_pct=float(cagr*100),
                max_dd_pct=float(dd.min()*100),
                peak_date=str(f.date.iloc[pk]) if e.iloc[pk] >= initial else "初始资金",
                trough_date=str(f.date.iloc[trough]), end_dd_pct=float(dd.iloc[-1]*100),
                underwater_days=longest, vol_pct=float(vol*np.sqrt(252)*100) if n > 1 else 0.,
                sharpe=float(ex.mean()/vol*np.sqrt(252)) if n > 1 and vol > 0 else None,
                sortino=float(ex.mean()/downside*np.sqrt(252)) if downside > 0 else None,
                calmar=float(cagr/abs(dd.min())) if dd.min() < 0 else None,
                ulcer=float(np.sqrt(np.mean((dd*100)**2))), fills=int(fills), fees=float(fees),
                avg_cash_pct=float((f.cash.astype(float)/e).mean()*100),
                defensive_days=int(defensive_days), defensive_pct=defensive_days/len(f)*100,
                daily_win_pct=float((observations > 0).mean()*100) if n else 0.,
                weekly_win_pct=float((weekly > 0).mean()*100) if len(weekly) else 0.)

def display_metrics(m):
    def num(key, suffix=""):
        return "—" if m[key] is None else f"{m[key]:.2f}{suffix}"
    return [num("end_equity", " 元"), num("profit", " 元"), num("return_pct", "%"),
            num("cagr_pct", "%"), num("max_dd_pct", "%"), m["peak_date"], m["trough_date"],
            num("end_dd_pct", "%"), str(m["underwater_days"]) + " 个交易日", num("vol_pct", "%"),
            num("sharpe"), num("sortino"), num("calmar"), num("ulcer"),
            f'{m["fills"]} 笔 / {m["fees"]:.2f} 元', num("avg_cash_pct", "%"),
            f'{m["defensive_days"]} 天 ({m["defensive_pct"]:.2f}%)',
            f'{m["daily_win_pct"]:.2f}% / {m["weekly_win_pct"]:.2f}%']
