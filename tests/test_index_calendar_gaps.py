from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from services import market_calendar as calendar
from services import position_timing as timing
from services import index_sources_eastmoney as source


@pytest.fixture(autouse=True)
def static_calendar_only():
    with patch.object(calendar, '_get_exchange_calendar', return_value=None):
        yield


@pytest.mark.parametrize('year,count', [(2022,242),(2023,242),(2024,242),(2025,243)])
def test_published_full_year_trading_day_counts(year, count):
    market = calendar.get_market_window('A股')
    days = pd.bdate_range(f'{year}-01-01', f'{year}-12-31')
    assert sum(not calendar.is_market_holiday(market, d.date()) for d in days) == count


@pytest.mark.parametrize('closed,reopen', [
    ('2022-09-12','2022-09-13'), ('2023-01-27','2023-01-30'),
    ('2024-02-09','2024-02-19'), ('2025-02-04','2025-02-05'),
])
def test_historical_holidays_and_next_open(closed, reopen):
    market = calendar.get_market_window('A股')
    assert calendar.is_market_holiday(market, date.fromisoformat(closed))
    assert not calendar.is_market_holiday(market, date.fromisoformat(reopen))


def history_since_2022():
    market = calendar.get_market_window('A股')
    days = [d for d in pd.bdate_range('2022-07-29','2026-09-10')
            if not calendar.is_market_holiday(market, d.date())]
    assert len(days) == 1000  # independently observed complete CSI500 cache
    return pd.DataFrame({'trade_date':days, 'close':100.})


def preview(history):
    now = datetime(2026,9,11,14,0,tzinfo=ZoneInfo('Asia/Shanghai'))
    quotes = {name:{'price':110.,'quote_time':now} for name in timing.POSITION_INDEX_TIMING_STRATEGIES}
    with patch.object(timing,'_load_position_index_timing_history',return_value=history):
        return timing.build_position_index_timing_table(realtime_quotes=quotes,market_now=now)


def test_multi_year_history_excludes_holidays_without_mutation():
    history = history_since_2022()
    original = history.copy(deep=True)
    rows = preview(history)
    assert (rows['择时判断'] == '买入').all()
    assert all('实时预判' in status for status in rows['数据状态'])
    pd.testing.assert_frame_equal(history, original)


def test_old_gap_outside_ma_window_does_not_block_preview():
    history = history_since_2022()
    history = history[history.trade_date != pd.Timestamp('2023-01-10')]
    rows = preview(history)
    assert (rows['择时判断'] == '买入').all()
    assert all('实时预判' in status for status in rows['数据状态'])


def test_old_gaps_allow_all_index_previews_without_fabricated_rows():
    history = history_since_2022()
    gaps = pd.to_datetime(['2026-04-03','2026-04-07','2026-05-25','2026-07-01'])
    history = history[~history.trade_date.isin(gaps)]
    original = history.copy(deep=True)
    rows = preview(history).set_index('代码')
    assert rows.loc['BK1158','择时判断'] == '买入'
    assert (rows['择时判断'] == '买入').all()
    pd.testing.assert_frame_equal(history, original)
    # An additional genuine missing session remains blocking.
    history = history[history.trade_date != pd.Timestamp('2026-09-10')]
    assert pd.isna(preview(history).set_index('代码').loc['BK1158','择时判断'])


def test_insufficient_ma_rows_still_do_not_produce_preview_signal():
    rows = preview(history_since_2022().tail(5))
    assert rows['择时判断'].isna().all()
    assert (rows['数据状态'] == '正式缓存不足').all()


def test_old_unknown_calendar_year_does_not_block_current_ma():
    history = pd.concat([pd.DataFrame({'trade_date':[pd.Timestamp('2021-12-31')],'close':[100.]}),history_since_2022()])
    rows = preview(history)
    assert (rows['择时判断'] == '买入').all()
    assert all('实时预判' in status for status in rows['数据状态'])
    assert all('正式历史缺少' not in status for status in rows['数据状态'])


@pytest.mark.parametrize('name,period', [('中证500',15), ('中证1000',30)])
def test_exact_ma_window_with_old_uncovered_history_can_preview(name, period):
    recent = pd.DataFrame({
        'trade_date': pd.bdate_range(end='2026-09-10', periods=period - 1),
        'close': 100.,
    })
    old = pd.DataFrame({
        'trade_date': pd.to_datetime([f'{year}-01-04' for year in range(2005,2022)]),
        'close': 50.,
    })
    history = pd.concat([old, recent], ignore_index=True)
    original = history.copy(deep=True)
    with patch('core.cache.save_dataset', side_effect=AssertionError('no cache writes')):
        row = preview(history).set_index('指数名称').loc[name]
    # Older data remains available to retain an already established holding.
    assert row['择时判断'] == '持有'
    assert row['对应均线'] == pytest.approx((100 * (period - 1) + 110) / period)
    assert '实时预判' in row['数据状态']
    pd.testing.assert_frame_equal(history, original)


def test_gap_only_in_ma30_window_blocks_csi1000_but_not_csi500():
    history = pd.DataFrame({
        'trade_date': pd.bdate_range(end='2026-09-10', periods=40), 'close': 100.,
    }).drop(index=20)
    rows = preview(history).set_index('代码')
    assert rows.loc['000905', '择时判断'] == '买入'
    assert pd.isna(rows.loc['000852', '择时判断'])
    assert '暂停择时预判' in rows.loc['000852', '数据状态']


def test_old_uncovered_year_also_emits_coverage_warning():
    calendar._warn_static_calendar_coverage.cache_clear()
    with patch.object(calendar.logger,'warning') as warn:
        calendar._warn_static_calendar_coverage('A股',2021)
        calendar._warn_static_calendar_coverage('A股',2022)
    assert warn.call_count == 1
    calendar._warn_static_calendar_coverage.cache_clear()


@pytest.mark.parametrize('hong_kong', [False,True])
def test_eastmoney_only_applies_hk_calendar_to_hk_instruments(hong_kong):
    dates = ['2026-04-03','2026-04-07','2026-05-25','2026-07-01','2026-07-02']
    response = SimpleNamespace(raise_for_status=lambda:None,
        json=lambda:{'data':{'klines':[f'{d},100,101' for d in dates]}})
    with patch('requests.Session.get',return_value=response), \
         patch.object(source,'append_eastmoney_latest_index_row',side_effect=lambda ak,df,*args,**kw:df), \
         patch.object(source,'build_export_df',side_effect=lambda df,*args,**kw:df):
        result = source.get_index_data_from_eastmoney_kline(
            '100.TEST' if hong_kong else '90.BK1158', '测试',
            akshare_hk_em_symbol='TEST' if hong_kong else None,
            akshare_board_symbol=None if hong_kong else 'BK1158')
    actual = pd.to_datetime(result.trade_date).dt.strftime('%Y-%m-%d').tolist()
    assert actual == (['2026-07-02'] if hong_kong else dates)
