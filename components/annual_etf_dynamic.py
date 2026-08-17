from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.cache import load_dataset, save_dataset
from core.paths import OUTPUT_DIR
from services.annual_etf_portfolio import (
    ALL_SLOTS,
    ANNUAL_CACHE_PERIOD,
    ANNUAL_CACHE_SOURCE,
    ANNUAL_DIVIDEND_CACHE_KEY,
    ANNUAL_DIVIDEND_DATA_TYPE,
    ANNUAL_RAW_DATA_TYPE,
    AnnualBacktestSettings,
    annual_raw_cache_key,
    dividends_for_symbol,
    fetch_annual_dividends,
    fetch_annual_etf_raw_history,
    load_index_family_config,
    load_registry,
    normalize_annual_market_data,
    preflight_annual_candidates,
    registry_frame,
    run_annual_etf_backtest,
    share_splits_for_symbol,
    validate_registry_against_whitelist,
)
from services.market_calendar import get_market_window, latest_completed_trade_date


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "config" / "annual_etf_registry_v1.csv"
WHITELIST_PATH = ROOT / "config" / "annual_etf_index_families.json"
RUNTIME_DIR = OUTPUT_DIR / "annual_etf_backtest"
AUDIT_MARKET_DIR = OUTPUT_DIR / "etf_portfolio_audit_20260727" / "market_data"

DIRECTION_LABELS = {
    "us_sp500": "标普500",
    "us_nasdaq": "纳斯达克100",
    "a_large": "A股核心宽基",
    "a_mid_small": "A股中小盘",
    "a_growth": "A股成长宽基",
    "smart_beta": "Smart Beta",
    "other_overseas": "非美国家或区域宽基",
    "gold": "黄金现货",
}
RESULT_TABLES = {
    "年度选择": "selections",
    "年度资格": "qualification",
    "参数遍历": "parameters",
    "实际交易": "trades",
    "实际迁移": "migrations",
    "方向贡献": "contribution",
    "年度收益": "yearly",
    "失败明细": "errors",
    "每日净值": "daily",
}


def _date_column(frame: pd.DataFrame) -> str | None:
    for column in ("日期", "trade_date", "date", "datetime"):
        if column in frame.columns:
            return column
    return None


def _completed_a_share_date():
    market = get_market_window("A股")
    if market is None:
        return pd.Timestamp.today().date()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    return latest_completed_trade_date(market, now)


def _filter_completed_rows(frame: pd.DataFrame, completed_date) -> pd.DataFrame:
    column = _date_column(frame)
    if column is None:
        raise ValueError("正式日线缺少日期列。")
    dates = pd.to_datetime(frame[column], errors="coerce").dt.date
    return frame.loc[dates <= completed_date].copy().reset_index(drop=True)


def _append_unseen_dates(existing: pd.DataFrame | None, fetched: pd.DataFrame) -> pd.DataFrame:
    if existing is None or existing.empty:
        return fetched.copy()
    old_column = _date_column(existing)
    new_column = _date_column(fetched)
    if old_column is None or new_column is None:
        raise ValueError("正式日线缺少日期列，拒绝覆盖原缓存。")
    old = existing.copy()
    new = fetched.copy()
    old["_merge_date"] = pd.to_datetime(old[old_column], errors="coerce").dt.normalize()
    new["_merge_date"] = pd.to_datetime(new[new_column], errors="coerce").dt.normalize()
    old_dates = set(old["_merge_date"].dropna())
    new = new[~new["_merge_date"].isin(old_dates)]
    return (
        pd.concat([old, new], ignore_index=True, sort=False)
        .sort_values("_merge_date")
        .drop(columns="_merge_date")
        .reset_index(drop=True)
    )


def _append_dividends(existing: pd.DataFrame | None, fetched: pd.DataFrame) -> pd.DataFrame:
    if existing is None or existing.empty:
        return fetched.drop_duplicates().reset_index(drop=True)
    return (
        pd.concat([existing, fetched], ignore_index=True, sort=False)
        .drop_duplicates(keep="first")
        .reset_index(drop=True)
    )


def _read_raw_fallback(record) -> tuple[pd.DataFrame | None, str]:
    audit_path = AUDIT_MARKET_DIR / f"{record.symbol}_raw.csv"
    if audit_path.exists():
        return pd.read_csv(audit_path), "既有ETF审计未复权缓存"
    exchange = record.exchange.upper()
    patterns = (
        f"fund_close_v2_{record.symbol}.{exchange}_none_*_1d.csv",
        f"fund_close_{record.symbol}.{exchange}_none_*_1d.csv",
    )
    for pattern in patterns:
        matches = sorted((ROOT / "data" / "raw" / "tickflow").glob(pattern))
        if matches:
            return pd.read_csv(matches[-1]), "既有TickFlow未复权缓存"
    return None, ""


def _load_dividends() -> tuple[pd.DataFrame, str]:
    cached, meta = load_dataset(
        ANNUAL_DIVIDEND_CACHE_KEY,
        ANNUAL_CACHE_SOURCE,
        ANNUAL_DIVIDEND_DATA_TYPE,
        ANNUAL_CACHE_PERIOD,
    )
    if cached is not None:
        timestamp = (meta or {}).get("last_update_time", "")
        return cached, f"年度缓存 {str(timestamp).replace('T', ' ')}"
    fallback = AUDIT_MARKET_DIR / "official_dividends.csv"
    if fallback.exists():
        return pd.read_csv(fallback), "既有ETF审计分红缓存"
    return pd.DataFrame(), "缺少分红缓存"


def _load_proxy_data(records, completed_date):
    proxy_data: dict[str, pd.DataFrame] = {}
    rows = []
    for record in records:
        if not record.proxy_path:
            continue
        path = Path(record.proxy_path)
        if not path.is_absolute():
            path = ROOT / path
        try:
            raw = pd.read_csv(path)
            raw = _filter_completed_rows(raw, completed_date)
            proxy_data[record.symbol] = normalize_annual_market_data(raw)
            rows.append(
                {
                    "代码": f"{record.symbol}代理",
                    "名称": record.tracked_index,
                    "方向": DIRECTION_LABELS.get(record.direction, record.direction),
                    "状态": "可读取",
                    "来源": str(path),
                    "行数": len(proxy_data[record.symbol]),
                    "首日": proxy_data[record.symbol]["trade_date"].min(),
                    "末日": proxy_data[record.symbol]["trade_date"].max(),
                    "错误": "",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "代码": f"{record.symbol}代理",
                    "名称": record.tracked_index,
                    "方向": DIRECTION_LABELS.get(record.direction, record.direction),
                    "状态": "缺口",
                    "来源": str(path),
                    "行数": 0,
                    "首日": pd.NaT,
                    "末日": pd.NaT,
                    "错误": str(exc),
                }
            )
    return proxy_data, pd.DataFrame(rows)


def _load_market_bundle(records, whitelist, completed_date):
    dividends, dividend_source = _load_dividends()
    market_data: dict[str, pd.DataFrame] = {}
    rows = []
    for record in records:
        raw, meta = load_dataset(
            annual_raw_cache_key(record.symbol),
            ANNUAL_CACHE_SOURCE,
            ANNUAL_RAW_DATA_TYPE,
            ANNUAL_CACHE_PERIOD,
        )
        source = "年度专用缓存"
        if raw is None:
            raw, source = _read_raw_fallback(record)
        try:
            if raw is None or raw.empty:
                raise ValueError("缺少未复权正式日线")
            raw = _filter_completed_rows(raw, completed_date)
            normalized = normalize_annual_market_data(
                raw,
                dividends_for_symbol(dividends, record.symbol),
                share_splits_for_symbol(whitelist, record.symbol),
            )
            market_data[record.symbol] = normalized
            rows.append(
                {
                    "代码": record.symbol,
                    "名称": record.name,
                    "方向": DIRECTION_LABELS.get(record.direction, record.direction),
                    "状态": "可读取",
                    "来源": source,
                    "行数": len(normalized),
                    "首日": normalized["trade_date"].min(),
                    "末日": normalized["trade_date"].max(),
                    "错误": "",
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "代码": record.symbol,
                    "名称": record.name,
                    "方向": DIRECTION_LABELS.get(record.direction, record.direction),
                    "状态": "缺口",
                    "来源": source,
                    "行数": 0,
                    "首日": pd.NaT,
                    "末日": pd.NaT,
                    "错误": str(exc),
                }
            )
    return market_data, pd.DataFrame(rows), dividends, dividend_source


def _network_fill(records, completed_date, start_year: int, refresh: bool, batch_size: int):
    rows = []
    candidates = []
    for record in records:
        cached, _meta = load_dataset(
            annual_raw_cache_key(record.symbol),
            ANNUAL_CACHE_SOURCE,
            ANNUAL_RAW_DATA_TYPE,
            ANNUAL_CACHE_PERIOD,
        )
        needs_history = cached is None or cached.empty
        if not needs_history and refresh:
            column = _date_column(cached)
            latest = (
                pd.to_datetime(cached[column], errors="coerce").max().date()
                if column is not None
                and pd.notna(pd.to_datetime(cached[column], errors="coerce").max())
                else None
            )
            needs_history = latest is None or latest < completed_date
        if needs_history:
            candidates.append(record)
    for record in candidates[:batch_size]:
        try:
            fetched = fetch_annual_etf_raw_history(
                record,
                start_date="20000101",
                end_date=pd.Timestamp(completed_date).strftime("%Y%m%d"),
            )
            fetched = _filter_completed_rows(fetched, completed_date)
            existing, _meta = load_dataset(
                annual_raw_cache_key(record.symbol),
                ANNUAL_CACHE_SOURCE,
                ANNUAL_RAW_DATA_TYPE,
                ANNUAL_CACHE_PERIOD,
            )
            merged = _append_unseen_dates(existing, fetched)
            normalize_annual_market_data(merged)
            save_dataset(
                annual_raw_cache_key(record.symbol),
                record.name,
                ANNUAL_CACHE_SOURCE,
                ANNUAL_RAW_DATA_TYPE,
                merged,
                ANNUAL_CACHE_PERIOD,
            )
            rows.append({"代码": record.symbol, "状态": "已补齐", "错误": ""})
        except Exception as exc:
            rows.append({"代码": record.symbol, "状态": "失败，保留原缓存", "错误": str(exc)})

    dividends, _meta = load_dataset(
        ANNUAL_DIVIDEND_CACHE_KEY,
        ANNUAL_CACHE_SOURCE,
        ANNUAL_DIVIDEND_DATA_TYPE,
        ANNUAL_CACHE_PERIOD,
    )
    dividend_years = []
    if dividends is None or dividends.empty:
        dividend_years = list(
            range(max(2005, start_year - 5), pd.Timestamp(completed_date).year + 1)
        )
    elif refresh:
        dividend_years = [pd.Timestamp(completed_date).year]
    if dividend_years:
        parts = []
        for year in dividend_years:
            try:
                parts.append(fetch_annual_dividends(year))
            except Exception as exc:
                rows.append({"代码": f"分红{year}", "状态": "失败，保留原缓存", "错误": str(exc)})
        if parts:
            merged = _append_dividends(dividends, pd.concat(parts, ignore_index=True, sort=False))
            save_dataset(
                ANNUAL_DIVIDEND_CACHE_KEY,
                "年度ETF官方分红",
                ANNUAL_CACHE_SOURCE,
                ANNUAL_DIVIDEND_DATA_TYPE,
                merged,
                ANNUAL_CACHE_PERIOD,
            )
    return pd.DataFrame(rows), max(0, len(candidates) - min(len(candidates), batch_size))


def _qualification_summary(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    summary = (
        frame.groupby(["year", "direction"], as_index=False)
        .agg(
            注册表候选数=("symbol", "size"),
            初筛合格数=("qualified_before_index_dedup", "sum"),
            代表ETF数=("qualified", "sum"),
            最高代理占比=("proxy_ratio_pct", "max"),
        )
        .rename(columns={"year": "年度", "direction": "方向"})
    )
    summary["方向"] = summary["方向"].map(DIRECTION_LABELS).fillna(summary["方向"])
    return summary


def _render_result(result, initial_capital: float) -> None:
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
                go.Scatter(x=daily["trade_date"], y=daily[column], mode="lines", name=label)
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
        ["年度选择", "年度资格", "参数", "迁移", "交易", "方向贡献", "年度收益", "失败明细", "每日净值"]
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
                    display["slot"] = display["slot"].map(DIRECTION_LABELS).fillna(display["slot"])
                if "direction" in display:
                    display["direction"] = display["direction"].map(DIRECTION_LABELS).fillna(display["direction"])
                st.dataframe(display, width="stretch", hide_index=True)

    st.subheader("下载")
    label = st.selectbox("CSV明细", list(RESULT_TABLES), key="annual_result_download_table")
    frame = getattr(result, RESULT_TABLES[label])
    st.download_button(
        "下载所选CSV",
        data=frame.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
        file_name=f"annual_etf_{RESULT_TABLES[label]}.csv",
        mime="text/csv",
    )
    st.download_button(
        "下载Markdown报告",
        data=result.report_markdown.encode("utf-8"),
        file_name="annual_etf_backtest_report.md",
        mime="text/markdown",
    )
    st.caption(f"检查点数据指纹：{result.fingerprint}")


def render_annual_dynamic_mode() -> None:
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
        records = load_registry(REGISTRY_PATH)
        whitelist = load_index_family_config(WHITELIST_PATH)
    except Exception as exc:
        st.error(f"年度ETF配置读取失败：{exc}")
        return
    registry_versions = pd.read_csv(REGISTRY_PATH)["registry_version"].dropna().unique().tolist()
    if len(registry_versions) != 1:
        st.error("年度ETF注册表必须且只能包含一个版本号。")
        return
    registry_version = str(registry_versions[0])
    whitelist_version = str(whitelist.get("version", ""))
    completed_date = _completed_a_share_date()

    with st.sidebar:
        st.subheader("年度动态组合参数")
        year_options = list(range(2019, pd.Timestamp(completed_date).year + 1))
        start_year = st.selectbox("起投年份", year_options, index=0, key="annual_start_year")
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
    settings = AnnualBacktestSettings(
        start_year=int(start_year),
        end_date=str(effective_end),
        initial_capital=float(initial_capital),
        commission_rate=float(commission_bp) / 10000,
        cash_annual_rate=float(cash_rate_pct) / 100,
        registry_version=registry_version,
        whitelist_version=whitelist_version,
    )

    registry_status = validate_registry_against_whitelist(records, whitelist)
    cols = st.columns(4)
    cols[0].metric("注册表版本", registry_version.rsplit("-", 1)[-1])
    cols[1].metric("快照日", str(pd.read_csv(REGISTRY_PATH)["snapshot_date"].iloc[0]))
    cols[2].metric("登记ETF", len(records))
    cols[3].metric("白名单内ETF", int(registry_status["registry_eligible"].sum()))
    with st.expander("查看注册表与白名单校验", expanded=False):
        st.dataframe(registry_frame(records), width="stretch", hide_index=True)
        invalid = registry_status[~registry_status["registry_eligible"]]
        if not invalid.empty:
            st.dataframe(invalid, width="stretch", hide_index=True)

    if st.button("1. 执行缓存只读预检", key="annual_preflight", type="primary"):
        st.session_state["annual_preflight_ready"] = True
    if not st.session_state.get("annual_preflight_ready", False):
        st.info("先执行缓存只读预检；此步骤不会联网或写入任何行情。")
        return

    market_data, data_status, _dividends, dividend_source = _load_market_bundle(
        records, whitelist, completed_date
    )
    proxy_data, proxy_status = _load_proxy_data(records, completed_date)
    if not proxy_status.empty:
        data_status = pd.concat([data_status, proxy_status], ignore_index=True)
    preflight = preflight_annual_candidates(
        records, whitelist, market_data, settings, proxy_data
    )
    st.markdown("#### 第一步：缓存只读预检")
    st.caption(f"分红来源：{dividend_source}；结束日：{effective_end}")
    st.dataframe(data_status, width="stretch", hide_index=True)
    summary = _qualification_summary(preflight.qualification)
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
            network_rows, remaining = _network_fill(
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
        st.caption(f"按当前选择仍有 {st.session_state.get('annual_network_remaining', 0)} 只待续补。")

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
    missing_slots = set(ALL_SLOTS) - start_slots
    if missing_slots:
        st.warning(
            "起投年度仍缺少合格方向："
            + "、".join(DIRECTION_LABELS.get(slot, slot) for slot in sorted(missing_slots))
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
            result = run_annual_etf_backtest(
                records,
                whitelist,
                market_data,
                settings,
                proxy_data=proxy_data,
                checkpoint_dir=RUNTIME_DIR / "checkpoints",
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
        _render_result(result, float(initial_capital))
    elif result is not None:
        st.info("参数已变化，请重新运行年度动态组合回测。")
