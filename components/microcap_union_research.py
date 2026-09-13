"""Cache-only display of the separately authorized retrospective estimate."""
from pathlib import Path
import json

import pandas as pd
import streamlit as st


def render_union_research():
    directory = Path(__file__).resolve().parents[1]/'output/microcap_union_estimate_20260101_20260908_v2'
    st.caption('固定候选池历史估算：使用2026-06-22至09-08快照并集，补入中设、如意、麦趣尔、莫高、美芝、龙韵；按历史当日ST及停牌标记过滤，戴帽前可入选。存在后期候选池偏差；股本按快照锚点或补充股票2025三季报固定估算，忽略期间股本变动。不是历史真实BK1158，也不是严格PIT回测数据。')
    required = ['report.json', 'daily_top20.csv', 'daily_summary.csv']
    if not all((directory/name).exists() for name in required):
        st.info('历史估算尚未生成完成。')
        return
    try:
        report = json.loads((directory/'report.json').read_text())
        summary = pd.read_csv(directory/'daily_summary.csv')
        top = pd.read_csv(directory/'daily_top20.csv', dtype={'代码': str})
    except (ValueError, OSError) as exc:
        st.error(f'历史估算读取失败：{exc}')
        return
    st.info(f"候选池 {report['candidate_count']} 只；覆盖 {summary['日期'].min()} 至 {summary['日期'].max()}，共 {report['days']} 个交易日；其中 {report['days_with_20']} 日有20只估算名单。")
    if report['failures']:
        st.warning(f"{len(report['failures'])}只股票日线未取到，详见下方报告。")
    st.line_chart(summary.set_index('日期')[['微盘20估算均值(亿元)']])
    selected = st.selectbox('查看估算名单日期', sorted(top['date'].unique(), reverse=True), key='microcap_union_date')
    view = top[top['date'].eq(selected)].copy()
    view['估算总市值(亿元)'] = view['estimated_market_cap_yuan']/1e8
    view = view.rename(columns={'strategy_rank':'过滤后排名','raw_market_cap_rank':'候选池原始排名',
        'close':'未复权收盘价','share_anchor_date':'股本锚点日期','effective_date':'最早使用日期',
        '名称':'快照名称'})
    st.dataframe(view[['过滤后排名','候选池原始排名','代码','快照名称','未复权收盘价',
        '估算总市值(亿元)','股本锚点日期','最早使用日期']], hide_index=True, use_container_width=True)
    st.caption('快照名称可能与历史当日名称不同；ST过滤使用当日日线标记。无日线可能是未上市、停牌或来源缺失，均不补价格。名单不包含次日开盘前资格复核，也不代表能以显示价格成交。')
    for name, label in [('daily_top200.csv','下载全部日期前200名估算名单'),
                        ('chart_metrics.csv','下载微盘20与第200名估算曲线'),
                        ('daily_top20.csv','下载全部日期微盘20估算名单'),
                        ('daily_summary.csv','下载每日覆盖与剔除统计'),
                        ('all_candidate_days.csv','下载全部候选日线与过滤原因')]:
        st.download_button(label, (directory/name).read_bytes(), file_name=name, mime='text/csv', key='union_'+name)
    with st.expander('每日覆盖及估算说明'):
        st.dataframe(summary, hide_index=True, use_container_width=True)
        st.json(report)
