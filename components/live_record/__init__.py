"""实盘记录页面组件。"""

from components.live_record.formatting import format_live_number, money
from components.live_record.history import render_live_symbol_pnl_history
from components.live_record.tables import (
    render_live_positions_table,
    render_live_symbol_history_table,
)
from components.live_record.trades import (
    render_live_trade_details,
    render_live_trade_form,
    render_live_trade_summary,
)
from components.live_record.valuation import (
    render_daily_close_pnl,
    render_live_return_calendar,
)

__all__ = [
    "format_live_number",
    "money",
    "render_daily_close_pnl",
    "render_live_positions_table",
    "render_live_return_calendar",
    "render_live_symbol_history_table",
    "render_live_symbol_pnl_history",
    "render_live_trade_details",
    "render_live_trade_form",
    "render_live_trade_summary",
]
