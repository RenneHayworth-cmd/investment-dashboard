"""Cache-only ABCD view; mutations occur only on explicit buttons."""
import json
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from core.ui import apply_plotly_layout
from services import microcap_rotation as service

def table(rows):
    frame=pd.DataFrame(rows)
    if frame.empty:
        st.info("暂无记录。")
        return
    for col in frame.columns:
        if pd.api.types.is_numeric_dtype(frame[col]):
            frame[col]=frame[col].map(lambda x:"—" if pd.isna(x) else f"{x:,.4f}" if "ratio" in col or "nav" in col else f"{x:,.2f}")
        elif frame[col].map(lambda x:isinstance(x,(dict,list))).any():
            frame[col]=frame[col].map(lambda x:json.dumps(x,ensure_ascii=False) if isinstance(x,(dict,list)) else x)
    aliases={"date":"日期","strategy":"策略","equity":"总资产","cash":"现金","quantity":"数量",
             "price":"价格","value":"市值","code":"代码","name":"名称","side":"方向","reason":"原因",
             "fee":"佣金","amount":"成交金额","cash_after":"成交后现金","pnl":"每日盈亏",
             "stale":"停牌沿用前值","signal_date":"信号日期","execution_date":"模拟执行日期",
             "metric":"指标","original":"原表","recomputed":"统一复算","return_pct":"累计收益(%)",
             "drawdown_pct":"回撤(%)","source":"来源","asof":"行情日期","status":"状态",
             "started_at":"开始时间","finished_at":"结束时间","message":"说明","job_name":"任务",
             "stock_ratio":"股票比例","etf_ratio":"ETF比例","cash_ratio":"现金比例"}
    frame=frame.rename(columns=aliases).replace({"buy":"买入","sell":"卖出"})
    st.dataframe(frame,hide_index=True,width="stretch")

def charts(rows):
    if not rows:
        return
    f=pd.DataFrame([{k:r[k] for k in ("date","strategy","equity")} for r in rows])
    f["equity"]=f.equity.astype(float)
    values=f.pivot(index="date",columns="strategy",values="equity").sort_index()
    common=values.dropna()
    if len(common)!=len(values):
        st.warning("策略数据未完全对齐；图表仅展示共同日期。")
        table(f.groupby("strategy").date.max().reset_index())
    if common.empty:
        st.info("暂无共同区间。")
        return
    st.caption(f"共同区间：{common.index[0]} 至 {common.index[-1]}；净值均以22万元为基准。")
    nav,dd=st.tabs(["净值","回撤"])
    for panel,drawdown in ((nav,False),(dd,True)):
        with panel:
            fig=go.Figure()
            for s in common.columns:
                y=(common[s]/common[s].cummax().clip(lower=220000)-1)*100 if drawdown else common[s]/220000
                fig.add_trace(go.Scatter(x=common.index,y=y,name=s+" "+service.NAMES[s],
                                        text=[f"{v:.2f}%" if drawdown else f"{v:.4f}" for v in y],
                                        hovertemplate="%{x}<br>%{text}<extra>%{fullData.name}</extra>"))
            apply_plotly_layout(fig,height=380)
            st.plotly_chart(fig,width="stretch")

def daily(view):
    accounts=view["accounts"]
    if not accounts:
        st.info("每日模拟尚未启用。启用后从第一个输入完整的收盘日开始，四账户各22万元。")
        st.caption("BK1158非ST最小20只；每只新仓约1万元；股票每笔2元，512890按成交金额万分之0.6；MA15±2.5%；不承接旧账户。")
        if st.button("启用每日模拟",type="primary"):
            service.initialize_simulation()
            st.success("四账户已启用，等待首个完整收盘日。")
            st.rerun()
        return
    latest=view["latest"]
    if not latest:
        st.info(f"已于 {accounts[0]['enabled_at'][:10]} 启用；今日收盘后自动模拟成交。")
        launch=view.get("launch")
        if launch:
            st.caption(f"首次执行 {launch['start_date']}；名单与信号依据 {launch['reference_date']} 已保存收盘数据。")
            for col,s in zip(st.columns(4),"ABCD"):
                with col:
                    with st.container(border=True):
                        st.markdown(f"**{s} · {service.NAMES[s]}**")
                        st.metric("起始资金","220,000.00 元")
                        plan=launch["states"][s]["plan"]
                        st.caption("今日收盘起算指数基准" if not plan else "今日计划："+{"stock":"买入微盘20","cash":"持有现金","etf":"买入512890"}[plan["mode"]])
            with st.expander("查看今日待执行名单"):
                table([dict(strategy=s,code=c,side="buy",execution_date=launch["start_date"])
                       for s in "BCD" for c in launch["states"][s]["plan"]["buys"]+
                       (["512890"] if launch["states"][s]["plan"]["buy_etf"] else [])])
        return
    if any(r.get("provisional") for r in latest.values()):
        st.warning("执行优先模拟：含未完全核验数据，收益仅供参考；缺价交易已跳过，前值估值会单独标记。")
        with st.expander("本次数据问题"):
            st.write(list(dict.fromkeys(w for r in latest.values() for w in r.get("warnings",[]))))
    a=latest["A"]
    first=min(r["date"] for r in view["daily"])
    st.caption(f"账户起点 {first} ｜ 数据截止 {a['date']} ｜ 最后成功更新 {a['completed_at']}")
    if a["date"]<view["target_date"]:
        st.warning(f"数据待补齐：最新完整交易日为 {view['target_date']}，目前保留 {a['date']} 结果。")
    selected=st.session_state.get("microcap_rotation_detail")
    if selected:
        if st.button("← 返回四策略对比"):
            st.session_state.pop("microcap_rotation_detail",None)
            st.rerun()
        r=latest[selected]
        st.subheader(selected+" · "+service.NAMES[selected])
        table([{"date":r["date"],"equity":r["equity"],"cash":r["cash"],"pnl":r["pnl"]}])
        tabs=st.tabs(["当前持仓","计划交易","模拟成交","未成交原因","每日盈亏","权益事件"])
        with tabs[0]:
            table([dict(code=c,**p) for c,p in r["positions"].items()])
            if selected=="A":
                st.caption("理论指数基准，不持有股票或ETF。")
        with tabs[1]:
            plan=r["plan"]
            if plan:
                st.caption(f"信号 {plan['signal_date']} → 模拟执行 {plan['execution_date']}；{plan['reason']}")
                table([dict(code=c,side="sell") for c in plan["sells"]]+[dict(code=c,side="buy") for c in plan["buys"]]+
                      ([dict(code="512890",side="buy")] if plan["buy_etf"] else []))
                st.caption("计划不等于成交；执行日正式价格、停牌、涨跌停及现金约束决定实际结果。")
        rows=[row for row in view["daily"] if row["strategy"]==selected]
        with tabs[2]:
            table([fill for row in rows for fill in row["fills"]])
        with tabs[3]:
            table([failure for row in rows for failure in row["failures"]])
        with tabs[4]:
            table([{k:row[k] for k in ("date","equity","cash","pnl","return_pct","drawdown_pct")} for row in rows])
        with tabs[5]:
            table([event for row in rows for event in row["events"]])
        charts(rows)
        return
    for col,s in zip(st.columns(4),"ABCD"):
        with col:
            r=latest[s]
            with st.container(border=True):
                st.markdown(f"**{s} · {service.NAMES[s]}**")
                st.metric("总资产",f"{float(r['equity']):,.2f} 元",f"{r['return_pct']:+.2f}%")
                maxdd=min(row["drawdown_pct"] for row in view["daily"] if row["strategy"]==s)
                st.caption(f"最大回撤 {maxdd:.2f}% ｜ 现金 {r['cash_ratio']:.2%}")
                st.caption("理论指数" if s=="A" else f"股票 {r['stock_ratio']:.2%} / ETF {r['etf_ratio']:.2%}")
                if s in ("C","D") and r["risk"] is None:
                    st.caption("择时状态待明确突破，暂持现金")
                if st.button("查看持仓与计划",key="rotation_"+s,width="stretch"):
                    st.session_state["microcap_rotation_detail"]=s
                    st.rerun()
    charts(view["daily"])

def research(view):
    report=view["research"]
    st.warning("后期候选池与固定股本估算，非严格历史时点回测；历史研究与新模拟独立。")
    if not report:
        st.info("尚未导入历史研究，请在「数据与运行」中选择交接包目录。")
        return
    st.caption("计算版本："+report["calculation_version"]+"；原始字段及输入哈希保留在导出中。")
    tabs=st.tabs(["统一复算","净值对比","成交与持仓","避险分段","审计说明"])
    with tabs[0]:
        table(report["comparison"])
        if report["differences"]:
            st.warning("原总表存在以下差异，以上展示使用统一复算值。")
            table(report["differences"])
    with tabs[1]:
        charts(report["daily"])
    with tabs[2]:
        strategy=st.selectbox("研究策略",["B","C","D"])
        table(report["trades"][strategy])
        with st.expander("原始周度持仓"):
            table(report["holdings"][strategy])
    with tabs[3]:
        table(report["periods"])
    with tabs[4]:
        for name,text in report["documents"].items():
            with st.expander(name):
                st.text(text)
        table(report["transforms"])

def operations(view):
    st.caption("预检只读缓存；更新按钮才会补必要数据并推进模拟。网页关闭仍可运行，依赖电脑开机、WSL及数据源。")
    left,right=st.columns(2)
    with left:
        if st.button("只读预检"):
            st.session_state["rotation_run"]=service.update_simulation(preflight=True)
    with right:
        if st.button("更新至最新完整交易日",type="primary"):
            with st.spinner("正在校验并更新…"):
                st.session_state["rotation_run"]=service.update_simulation(refresh=True)
            st.rerun()
    run=st.session_state.get("rotation_run")
    if run:
        st.info(run["status"]+"："+run["message"])
        table(run.get("gaps",[]))
    with st.expander("导入历史研究"):
        path=st.text_input("交接包在WSL中的目录",placeholder="/mnt/c/Users/…/ABCD策略全景审计交接包")
        if st.button("核验并导入研究档案",disabled=not path):
            report=service.import_research(path)
            st.success(f"已核验72项展示值；发现 {len(report['differences'])} 处原表差异，展示使用复算结果。")
            st.rerun()
    with st.expander("数据证据与缺口"):
        st.caption("自动运行无需上传文件。此处仅作补充核验；缺价跳过交易，其他缺项标记待核验后继续模拟。")
        uploaded=st.file_uploader("导入当日核验记录",type=["json"],key="rotation_evidence")
        if uploaded and st.button("保存核验记录"):
            from services.microcap_rotation_store import process_lock
            with process_lock():
                day=service.save_evidence(uploaded.getvalue().decode("utf-8-sig"))
            st.success(day+" 核验记录已保存；请再次预检。")
        from services.microcap import load_microcap_constituent_snapshots
        snapshots,_=load_microcap_constituent_snapshots()
        if snapshots is not None and not snapshots.empty:
            st.caption("缓存快照最新日期："+str(snapshots["快照日期"].max()))
        st.caption("执行优先版本会保留数据问题明细；缺价持仓沿用前值参考估值，未核验权益事件可能影响收益。")
    from core.db import list_jobs
    jobs=list_jobs(100)
    if not jobs.empty:
        table(jobs[jobs.job_name.str.contains("ABCD",na=False)].to_dict("records"))
    export={k:v for k,v in view.items() if k!="accounts"}
    # Explicit numeric serialization precision; raw strings preserve money precision.
    payload=pd.Series([export]).to_json(orient="values",force_ascii=False,double_precision=8)[1:-1]
    st.download_button("下载结果与输入清单",payload.encode("utf-8"),"microcap_rotation.json","application/json")

def render_rotation():
    st.subheader("微盘20 · ABCD策略")
    st.caption("执行优先版本：缺项跳过或标记待核验，不要求手工上传证据。A为无摩擦指数基准；B/C/D每笔实际模拟成交收取2元，未计税费和滑点。所有成交均为模拟。")
    try:
        view=service.read_strategy_view()
        mode=st.segmented_control("策略视图",["每日模拟","历史研究","数据与运行"],default="每日模拟",key="rotation_view")
        {"每日模拟":daily,"历史研究":research,"数据与运行":operations}[mode or "每日模拟"](view)
    except Exception as exc:
        st.error("ABCD读取或更新失败："+str(exc))
