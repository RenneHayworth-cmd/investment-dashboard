"""Strategy table and formal daily cumulative P&L curves."""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.ui import apply_plotly_layout
from services.futures_live_strategy import STRATEGIES, build_strategy_analysis


def render_strategy_analysis(close_daily, settlement_daily):
    st.subheader('策略收益分析')
    mode = 'settlement' if st.session_state.get('futures_live_pnl_mode', '盯市') == '盯市' else 'close'
    label = '盯市' if mode == 'settlement' else '收盘'
    result = build_strategy_analysis(settlement_daily if mode == 'settlement' else close_daily,
                                     valuation_mode=mode)
    st.caption(f'沿用账户{label}口径，金额单位：元。铁矿石卖 Put、履约接货和后续换月统一计入铁矿石滚贴水。')
    if result['summary'].empty:
        st.info('尚无可共同核对的策略估值日期，需补齐正式价格；账户级手工盈亏不会分摊为策略收益。')
    else:
        st.caption(f"共同估值日期：{result['date']}。已实现与未实现均为扣费前金额，净盈亏已扣列示费用。")
        account_daily = settlement_daily if mode == 'settlement' else close_daily
        if result['date'] < str(account_daily['date'].max()):
            st.warning(f"策略表停留在 {result['date']}：其后尚无可共同核对的完整策略估值。")
        display = result['summary'].rename(columns={
            'strategy': '策略', 'realized_pnl': '已实现盈亏', 'floating_pnl': '未实现盈亏',
            'fee': '费用合计', 'net_pnl': '净盈亏', 'positions': '当前持仓'})
        st.dataframe(display, hide_index=True, width='stretch', column_config={
            key: st.column_config.NumberColumn(format='%.2f')
            for key in ['已实现盈亏', '未实现盈亏', '费用合计', '净盈亏']})
        with st.expander('查看与账户盈亏的核对'):
            st.dataframe(result['reconciliation'], hide_index=True, width='stretch',
                         column_config={'金额': st.column_config.NumberColumn(format='%.2f')})
            st.caption('玉米不进入策略表和曲线，仅保留核对金额。公共费用不强行分摊；盯市不扣申报费及其他账户级费用，收盘扣全部费用。')
        residual = float(result['reconciliation'].iloc[3]['金额'])
        if abs(residual) > .05:
            st.warning(f'尚有 {residual:,.2f} 元账户盈亏未分配，已单列核对差额，未计入策略收益。')
    daily = result['daily']
    if not daily.empty:
        gaps = daily[daily['strategy'].isin(STRATEGIES) & daily['status'].ne('完整')]
        if not gaps.empty:
            latest = gaps.iloc[-1]
            st.caption(f"历史价格缺口保留为曲线断点；例如 {latest['date']} {latest['strategy']}：{latest['missing_contracts']}。")
        chosen = st.multiselect('显示策略曲线', list(STRATEGIES), default=list(STRATEGIES),
                                key='futures_live_strategy_curves')
        figure = go.Figure()
        for strategy in chosen:
            curve = daily[daily['strategy'].eq(strategy)]
            figure.add_trace(go.Scatter(x=curve['date'], y=pd.to_numeric(curve['net_pnl'], errors='coerce'),
                                       name=strategy, mode='lines', connectgaps=False,
                                       hovertemplate='%{x}<br>累计净盈亏：%{y:,.2f} 元<extra>%{fullData.name}</extra>'))
        if chosen:
            apply_plotly_layout(figure)
            figure.update_yaxes(title_text='累计净盈亏（元）')
            st.plotly_chart(figure, width='stretch', config={'displayModeBar': False})
        st.caption('未分配策略本金，不计算策略收益率或净值；仅使用本地正式价格，缺失日期不插值。')
    if not result['allocations'].empty:
        with st.expander('查看成交策略归属'):
            st.caption('已有策略标注优先；未标注跨期腿按同日十分钟内最近成交配对。无法配对的铁矿石多头沿用滚贴水口径。')
            allocation = result['allocations']
            allocation = allocation[allocation['strategy'].isin(STRATEGIES)]
            st.dataframe(allocation[['trade_date', 'trade_time', 'contract', 'buy_sell', 'open_close', 'quantity', 'price', 'fee', 'strategy', 'attribution']].rename(columns={
                'trade_date': '交易日', 'trade_time': '时间', 'contract': '合约', 'buy_sell': '买卖',
                'open_close': '开平', 'quantity': '手数', 'price': '价格', 'fee': '手续费',
                'strategy': '策略', 'attribution': '归属依据'}), hide_index=True, width='stretch')
