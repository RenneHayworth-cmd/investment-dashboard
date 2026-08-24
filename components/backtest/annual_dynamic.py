from __future__ import annotations

import pandas as pd
import streamlit as st

from .annual_config import (
    DIRECTION_LABELS,
    REGISTRY_PATH,
    RUNTIME_DIR,
    WHITELIST_PATH,
)


def render_annual_dynamic_mode(namespace) -> None:
    """Render the annual workflow using dependencies from its compatibility facade."""
    st.subheader("历史年度 ETF 动态组合回测")
    st.warning(
        "候选池是注册表快照日仍上市的沪深ETF，因此明确存在生存者偏差；"
        "结果不得理解为已经消除退市产品影响。"
    )
    st.caption(
        "每年只使用上一年末前可获得的数据；年度换选不强制卖出旧ETF，"
        "旧仓等待原信号退出后迁往最新年度目标。"
    )

    try:
        records = namespace["load_registry"](namespace["REGISTRY_PATH"])
        whitelist = namespace["load_index_family_config"](
            namespace["WHITELIST_PATH"]
        )
    except Exception as exc:
        st.error(f"年度ETF配置读取失败：{exc}")
        return
    registry_versions = (
        pd.read_csv(namespace["REGISTRY_PATH"])["registry_version"]
        .dropna()
        .unique()
        .tolist()
    )
    if len(registry_versions) != 1:
        st.error("年度ETF注册表必须且只能包含一个版本号。")
        return
    registry_version = str(registry_versions[0])
    whitelist_version = str(whitelist.get("version", ""))
    completed_date = namespace["_completed_a_share_date"]()

    with st.sidebar:
        st.subheader("年度动态组合参数")
        year_options = list(range(2019, pd.Timestamp(completed_date).year + 1))
        start_year = st.selectbox(
            "起投年份", year_options, index=0, key="annual_start_year"
        )
        requested_end = st.date_input(
            "结束日期",
            value=completed_date,
            max_value=pd.Timestamp.today().date(),
            key="annual_end_date",
        )
        initial_capital = st.number_input(
            "初始资金",
            min_value=10000.0,
            value=500000.0,
            step=50000.0,
            key="annual_initial_capital",
        )
        commission_bp = st.number_input(
            "单边手续费（万分之）",
            min_value=0.0,
            value=0.6,
            step=0.1,
            key="annual_commission_bp",
        )
        cash_rate_pct = st.number_input(
            "现金年利率（%）",
            min_value=0.0,
            value=1.5,
            step=0.1,
            key="annual_cash_rate_pct",
        )
        refresh_existing = st.checkbox(
            "联网时检查已有缓存增量",
            value=False,
            key="annual_refresh_existing",
        )
        batch_size = st.number_input(
            "每批联网ETF数",
            min_value=1,
            max_value=12,
            value=6,
            step=1,
            key="annual_batch_size",
        )

    effective_end = min(pd.Timestamp(requested_end).date(), completed_date)
    if effective_end != requested_end:
        st.info(f"结束日期已收敛到最新完成正式交易日：{effective_end}")
    if effective_end.year < int(start_year):
        st.error("结束日期不能早于起投年份。")
        return
    settings = namespace["AnnualBacktestSettings"](
        start_year=int(start_year),
        end_date=str(effective_end),
        initial_capital=float(initial_capital),
        commission_rate=float(commission_bp) / 10000,
        cash_annual_rate=float(cash_rate_pct) / 100,
        registry_version=registry_version,
        whitelist_version=whitelist_version,
    )

    registry_status = namespace["validate_registry_against_whitelist"](
        records, whitelist
    )
    cols = st.columns(4)
    cols[0].metric("注册表版本", registry_version.rsplit("-", 1)[-1])
    cols[1].metric(
        "快照日",
        str(pd.read_csv(namespace["REGISTRY_PATH"])["snapshot_date"].iloc[0]),
    )
    cols[2].metric("登记ETF", len(records))
    cols[3].metric("白名单内ETF", int(registry_status["registry_eligible"].sum()))
    with st.expander("查看注册表与白名单校验", expanded=False):
        st.dataframe(namespace["registry_frame"](records), width="stretch", hide_index=True)
        invalid = registry_status[~registry_status["registry_eligible"]]
        if not invalid.empty:
            st.dataframe(invalid, width="stretch", hide_index=True)

    if st.button("1. 执行缓存只读预检", key="annual_preflight", type="primary"):
        st.session_state["annual_preflight_ready"] = True
    if not st.session_state.get("annual_preflight_ready", False):
        st.info("先执行缓存只读预检；此步骤不会联网或写入任何行情。")
        return

    market_data, data_status, _dividends, dividend_source = namespace[
        "_load_market_bundle"
    ](records, whitelist, completed_date)
    proxy_data, proxy_status = namespace["_load_proxy_data"](
        records, completed_date
    )
    if not proxy_status.empty:
        data_status = pd.concat([data_status, proxy_status], ignore_index=True)
    preflight = namespace["preflight_annual_candidates"](
        records, whitelist, market_data, settings, proxy_data
    )
    st.markdown("#### 第一步：缓存只读预检")
    st.caption(f"分红来源：{dividend_source}；结束日：{effective_end}")
    st.dataframe(data_status, width="stretch", hide_index=True)
    summary = namespace["_qualification_summary"](preflight.qualification)
    if summary.empty:
        st.warning("本地正式行情不足，尚不能形成年度资格表。")
    else:
        st.dataframe(summary, width="stretch", hide_index=True)
        with st.expander("查看逐年逐ETF资格、排除原因和代理占比", expanded=False):
            st.dataframe(preflight.qualification, width="stretch", hide_index=True)
    if not preflight.errors.empty:
        st.dataframe(preflight.errors, width="stretch", hide_index=True)

    st.markdown("#### 第二步：确认后分批联网补齐")
    confirm_network = st.checkbox(
        "我确认本次操作可以联网，并只把新增正式日线和分红追加到年度专用本地缓存",
        key="annual_network_confirm",
    )
    if st.button(
        "2. 联网补齐下一批",
        disabled=not confirm_network,
        key="annual_network_fill",
    ):
        with st.spinner("正在分批补齐未复权正式日线和分红；失败不会覆盖原缓存..."):
            network_rows, remaining = namespace["_network_fill"](
                records,
                completed_date,
                int(start_year),
                bool(refresh_existing),
                int(batch_size),
            )
        st.session_state["annual_network_rows"] = network_rows
        st.session_state["annual_network_remaining"] = remaining
        st.session_state.pop("annual_backtest_result", None)
        st.rerun()
    network_rows = st.session_state.get("annual_network_rows")
    if isinstance(network_rows, pd.DataFrame) and not network_rows.empty:
        st.dataframe(network_rows, width="stretch", hide_index=True)
        st.caption(
            f"按当前选择仍有 {st.session_state.get('annual_network_remaining', 0)} 只待续补。"
        )

    st.markdown("#### 第三步：运行回测")
    start_slots = set()
    if not preflight.qualification.empty:
        start_slots = set(
            preflight.qualification.loc[
                (preflight.qualification["year"] == int(start_year))
                & preflight.qualification["qualified"],
                "direction",
            ]
        )
    missing_slots = set(namespace["ALL_SLOTS"]) - start_slots
    if missing_slots:
        st.warning(
            "起投年度仍缺少合格方向："
            + "、".join(
                namespace["DIRECTION_LABELS"].get(slot, slot)
                for slot in sorted(missing_slots)
            )
        )
    run_clicked = st.button(
        "3. 运行年度动态组合回测",
        type="primary",
        disabled=bool(missing_slots),
        key="annual_run_backtest",
    )
    if run_clicked:
        progress = st.progress(0.0)
        status = st.empty()

        def update_progress(label: str, fraction: float) -> None:
            status.caption(label)
            progress.progress(min(1.0, max(0.0, float(fraction))))

        try:
            result = namespace["run_annual_etf_backtest"](
                records,
                whitelist,
                market_data,
                settings,
                proxy_data=proxy_data,
                checkpoint_dir=namespace["RUNTIME_DIR"] / "checkpoints",
                progress_callback=update_progress,
            )
            st.session_state["annual_backtest_result"] = result
            st.session_state["annual_backtest_settings"] = settings
            status.caption("四阶段处理完成")
        except Exception as exc:
            st.error(f"年度动态组合回测失败：{exc}")
            return

    result = st.session_state.get("annual_backtest_result")
    result_settings = st.session_state.get("annual_backtest_settings")
    if result is not None and result_settings == settings:
        namespace["_render_result"](result, float(initial_capital))
    elif result is not None:
        st.info("参数已变化，请重新运行年度动态组合回测。")


__all__ = ["render_annual_dynamic_mode"]
