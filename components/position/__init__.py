"""持仓分析页面组件。"""

from components.position.cards_tables import (
    delta_value,
    metric_row,
    primary_value,
    render_etf_operation_guidance,
    render_etf_timing_table,
    render_position_card,
    render_position_cards,
    render_summary_table,
)
from components.position.coordinator import render_position_page
from components.position.details import (
    render_etf_detail,
    render_option_detail,
    render_position_detail,
    render_spread_detail,
)
from components.position.formatting import (
    build_overview_table,
    clear_position_detail,
    display_digits,
    display_position_code,
    filter_range,
    format_etf_table_value,
    format_metric_for_item,
    format_number,
    format_pct,
    get_query_position_detail,
    position_key,
    rolling_annual_label,
    round_numeric_columns,
)
from components.position.realtime import render_etf_timing_section_impl
from components.position.performance import render_position_timing_performance

__all__ = [
    "build_overview_table",
    "clear_position_detail",
    "delta_value",
    "display_digits",
    "display_position_code",
    "filter_range",
    "format_etf_table_value",
    "format_metric_for_item",
    "format_number",
    "format_pct",
    "get_query_position_detail",
    "metric_row",
    "position_key",
    "primary_value",
    "render_etf_detail",
    "render_etf_operation_guidance",
    "render_etf_timing_section_impl",
    "render_etf_timing_table",
    "render_option_detail",
    "render_position_card",
    "render_position_cards",
    "render_position_detail",
    "render_position_page",
    "render_position_timing_performance",
    "render_spread_detail",
    "render_summary_table",
    "rolling_annual_label",
    "round_numeric_columns",
]
