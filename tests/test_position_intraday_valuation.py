"""Pure mark-to-market of completed holdings, never today's preview orders."""
from copy import deepcopy
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from services.position_performance import (
    PositionTimingPerformanceResult,
    build_position_timing_intraday_valuation as value,
)

NOW = datetime(2026, 8, 10, 14, 52, tzinfo=ZoneInfo('Asia/Shanghai'))


def formal():
    return PositionTimingPerformanceResult(
        daily=pd.DataFrame([{'日期': '2026-08-07', '账户资产': 500000., '持仓市值': 5000., '每日盈亏': 123.}]),
        positions=pd.DataFrame([
            {'代码': '159501', '持仓数量': 100, '最新价': 10., '成本价': 8.},
            {'代码': '510500', '持仓数量': 200, '最新价': 20., '成本价': 25.},
        ]),
        trades=pd.DataFrame([{'代码': '159501', '操作': '买入', '数量': 100}]),
    )


def quotes():
    return {
        '159501.SZ': {'price': 11., 'previous_close': 99., 'quote_time': NOW},
        '510500': {'price': 18., 'previous_close': 99., 'quote_time': NOW},
        '588000': {'price': 999., 'quote_time': NOW},  # unheld, ignored
    }


def test_exact_formal_quantity_and_close_cash_unchanged_no_writes():
    base, live = formal(), quotes()
    before, quote_before = deepcopy(base), deepcopy(live)
    with patch('core.cache.save_dataset', side_effect=AssertionError('cache write')), \
         patch('sqlite3.connect', side_effect=AssertionError('database access')), \
         patch.object(pd.DataFrame, 'to_csv', side_effect=AssertionError('CSV write')), \
         patch('requests.sessions.Session.request', side_effect=AssertionError('network')):
        result = value(base, live, market_now=NOW)
    assert result.available and result.complete
    assert result.daily_pnl == -300.  # 100*(11-10) + 200*(18-20)
    assert [row['当日盈亏'] for row in result.by_symbol] == [100., -400.]
    assert result.estimated_assets == 499700.
    assert result.daily_return_pct == pytest.approx(-0.06)
    first = result.by_symbol[0]
    assert first['持仓市值'] == 1100.
    assert first['浮动盈亏'] == 300.
    assert first['浮动收益率(%)'] == pytest.approx(37.5)
    assert first['当日收益率(%)'] == pytest.approx(10.)
    assert first['账户权重(%)'] == pytest.approx(1100 / 499700 * 100)
    assert first['成本价'] == 8.
    assert first['最新价'] == 11.
    assert result.formal_date == '2026-08-07'
    assert result.quote_time == '2026-08-10 14:52:00'
    assert result.mode == 'intraday'
    for field in ('daily', 'trades', 'positions', 'components'):
        pd.testing.assert_frame_equal(getattr(base, field), getattr(before, field))
    assert live == quote_before


@pytest.mark.parametrize('bad', [
    {}, {'price': 0, 'quote_time': NOW}, {'price': -1, 'quote_time': NOW},
    {'price': float('inf'), 'quote_time': NOW}, {'price': None, 'quote_time': NOW},
    {'price': pd.NA, 'quote_time': NOW},
    {'price': 18, 'quote_time': '2026-08-07 14:52:00'},
    {'price': 18, 'quote_time': 'invalid'}, {'price': 18, 'quote_time': NOW.replace(minute=53)},
])
def test_incomplete_prices_never_return_partial_pnl(bad):
    live = quotes()
    live['510500'] = bad
    result = value(formal(), live, market_now=NOW)
    assert not result.complete and not result.available
    assert result.missing_codes == ['510500']
    assert result.daily_pnl is None and result.estimated_assets is None
    assert result.by_symbol[0]['当日盈亏'] == 100.
    assert result.by_symbol[1]['当日盈亏'] is None
    assert result.by_symbol[1]['持仓市值'] is None
    assert result.by_symbol[0]['账户权重(%)'] is None


def test_closed_today_still_has_formal_pnl_including_sell_fee():
    from services.position_performance import _build_current_positions
    trades = pd.DataFrame([
        {'日期':'2026-08-06','代码':'159501','交易标的':'159501','操作':'买入','份额':100,'成交金额':1000.,'手续费':1.},
        {'日期':'2026-08-07','代码':'159501','交易标的':'159501','操作':'卖出','份额':100,'成交金额':1100.,'手续费':1.},
    ])
    histories = {'159501':pd.DataFrame({'trade_date':pd.to_datetime(['2026-08-06','2026-08-07']),'close':[10.,11.]})}
    args = dict(valuation_date=pd.Timestamp('2026-08-07'),account_assets=500098.)
    assert _build_current_positions(trades,histories,**args).empty
    detail = _build_current_positions(trades,histories,include_closed_today=True,**args)
    assert detail.iloc[0]['持仓数量'] == 0
    assert detail.iloc[0]['当日盈亏'] == 99.


@pytest.mark.parametrize('minute', [0, 4, 6, 30])
def test_retained_quotes_remain_estimates_until_formal_complete(minute):
    result = value(formal(), quotes(), market_now=NOW.replace(hour=15, minute=minute))
    assert result.mode == 'pending_close'
    assert result.complete and result.daily_pnl == -300.


def test_today_formal_only_after_confirmation_time():
    base = formal()
    base.daily.loc[0, '日期'] = '2026-08-10'
    assert not value(base, quotes(), market_now=NOW.replace(hour=15, minute=0)).complete
    result = value(base, quotes(), market_now=NOW.replace(hour=15, minute=6))
    assert result.mode == 'formal' and result.complete
    assert not result.available and result.daily_pnl is None
    assert base.daily.iloc[-1]['每日盈亏'] == 123.


def test_stale_formal_baseline_not_a_today_pnl():
    base = formal()
    base.daily.loc[0, '日期'] = '2026-08-06'
    result = value(base, quotes(), market_now=NOW)
    assert not result.complete and result.daily_pnl is None
    assert result.warnings


def test_full_cash_is_zero_without_requiring_unheld_quotes():
    base = formal()
    base.positions = pd.DataFrame()
    base.daily.loc[0, '持仓市值'] = 0
    result = value(base, {}, market_now=NOW)
    assert result.complete and result.daily_pnl == 0
    assert result.estimated_assets == 500000.


@pytest.mark.parametrize('now', [NOW.replace(hour=8), NOW.replace(day=9)])
def test_before_open_and_nontrading_days_use_last_formal(now):
    result = value(formal(), quotes(), market_now=now)
    assert result.mode == 'formal' and not result.available


def test_utc_quote_is_converted_to_shanghai():
    live = quotes()
    live['510500']['quote_time'] = '2026-08-10T06:50:00+00:00'
    assert value(formal(), live, market_now=NOW).quote_time == '2026-08-10 14:50:00'


def test_empty_or_invalid_formal_result_is_unavailable():
    for base in (PositionTimingPerformanceResult(), PositionTimingPerformanceResult(errors=['缺数据'])):
        assert not value(base, quotes(), market_now=NOW).complete
