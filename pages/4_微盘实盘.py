"""独立微盘A股实盘记账页。"""
import streamlit as st

from components.microcap_live import (
    render_microcap_cash_flows,
    render_microcap_dashboard,
    render_microcap_fee_settings,
    render_microcap_import,
    render_microcap_position_adjustments,
    render_microcap_rotation_monitor,
    render_microcap_trades,
)
from core.db import init_db
from core.ui import apply_global_style, render_page_header

st.set_page_config(page_title="微盘实盘", layout="wide")
init_db()
apply_global_style()
render_page_header(
    "微盘实盘",
    "独立记录实际A股成交、资金和持仓权益事件；不连接券商、不自动下单、不并入ABCD模拟账户。",
    eyebrow="Microcap Live Ledger",
)

@st.fragment(run_every="120s")
def _account_dashboard():
    render_microcap_dashboard()

account_tab, rotation_tab, trades_tab, flows_tab, import_tab, settings_tab = st.tabs(
    ["账户总览", "轮动监控", "成交与持仓", "资金与权益", "交割单导入", "费用设置"]
)
with account_tab:
    _account_dashboard()
with rotation_tab:
    render_microcap_rotation_monitor()
with trades_tab:
    render_microcap_trades()
with flows_tab:
    render_microcap_cash_flows()
    st.divider()
    render_microcap_position_adjustments()
with import_tab:
    render_microcap_import()
with settings_tab:
    render_microcap_fee_settings()
