from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from .annual_config import DIRECTION_LABELS, RESULT_TABLES


def render_result(
    result,
    initial_capital: float,
    *,
    direction_labels=DIRECTION_LABELS,
    result_tables=RESULT_TABLES,
) -> None:
    st.subheader("回测结果")
    summary = result.summary.copy()
    summary_display = summary.rename(
        columns={
            "series": "结果",
            "start_date": "实际开始",
            "end_date": "实际结束",
            "final_value": "期末资产",
            "net_profit": "净赚金额",
            "total_return_pct": "累计收益(%)",
            "annual_return_pct": "年化收益(%)",
            "max_drawdown_pct": "最大回撤(%)",
            "longest_underwater_days": "最长水下期(自然日)",
            "annual_volatility_pct": "年化波动(%)",
            "sharpe_ratio": "夏普",
            "trade_count": "交易次数",
            "commission_cost": "手续费",
        }
    )
    st.dataframe(summary_display, width="stretch", hide_index=True)

    main = summary.iloc[0]
    metrics = st.columns(6)
    metrics[0].metric("累计收益", f"{float(main['total_return_pct']):.2f}%")
    metrics[1].metric("年化收益", f"{float(main['annual_return_pct']):.2f}%")
    metrics[2].metric("净赚金额", f"{float(main['net_profit']):,.2f}")
    metrics[3].metric("最大回撤", f"{float(main['max_drawdown_pct']):.2f}%")
    metrics[4].metric("最长水下期", f"{int(main['longest_underwater_days'])}日")
    metrics[5].metric("夏普", f"{float(main['sharpe_ratio']):.2f}")

    daily = result.daily.copy()
    figure = go.Figure()
    series = {
        "main_value": "年度动态组合",
        "annual_hold_value": "年度选择一直持有",
        "parking_value": "全部持有512890",
        "next_close_value": "次日收盘压力",
    }
    for column, label in series.items():
        if column in daily:
            figure.add_trace(
                go.Scatter(
                    x=daily["trade_date"], y=daily[column], mode="lines", name=label
                )
            )
    figure.update_layout(
        height=460,
        xaxis_title="交易日",
        yaxis_title="组合资产（元）",
        hovermode="x unified",
        legend=dict(orientation="h", y=1.08),
    )
    st.plotly_chart(figure, width="stretch")
    st.caption(
        f"初始资金 {initial_capital:,.2f} 元；主结果为同日收盘理想化成交，"
        "压力结果冻结完全相同的年度选择与参数，仅延后到下一交易日收盘成交。"
    )

    tabs = st.tabs(
        [
            "年度选择",
            "年度资格",
            "参数",
            "迁移",
            "交易",
            "方向贡献",
            "年度收益",
            "失败明细",
            "每日净值",
        ]
    )
    frames = [
        result.selections,
        result.qualification,
        result.parameters,
        result.migrations,
        result.trades,
        result.contribution,
        result.yearly,
        result.errors,
        result.daily,
    ]
    for tab, frame in zip(tabs, frames):
        with tab:
            if frame is None or frame.empty:
                st.info("暂无明细。")
            else:
                display = frame.copy()
                if "slot" in display:
                    display["slot"] = (
                        display["slot"]
                        .map(direction_labels)
                        .fillna(display["slot"])
                    )
                if "direction" in display:
                    display["direction"] = (
                        display["direction"]
                        .map(direction_labels)
                        .fillna(display["direction"])
                    )
                st.dataframe(display, width="stretch", hide_index=True)

    st.subheader("下载")
    label = st.selectbox(
        "CSV明细", list(result_tables), key="annual_result_download_table"
    )
    frame = getattr(result, result_tables[label])
    st.download_button(
        "下载所选CSV",
        data=frame.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
        file_name=f"annual_etf_{result_tables[label]}.csv",
        mime="text/csv",
    )
    st.download_button(
        "下载Markdown报告",
        data=result.report_markdown.encode("utf-8"),
        file_name="annual_etf_backtest_report.md",
        mime="text/markdown",
    )
    st.caption(f"检查点数据指纹：{result.fingerprint}")


__all__ = ["render_result"]
