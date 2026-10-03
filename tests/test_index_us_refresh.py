from datetime import datetime, timedelta
from threading import Event, Thread
from unittest.mock import patch
from zoneinfo import ZoneInfo
import pytest
import pandas as pd
from pathlib import Path
from streamlit.testing.v1 import AppTest

from services import index_us_refresh as us
ir=us.realtime

def test_page_renders_cache_first_and_only_eligible_us_quotes_then_reuses_across_sessions():
    # Import before patching core.cache so lazy module initialization cannot
    # capture this test's synthetic report loader for subsequent page tests.
    import services.taoxi_microcap_index

    source=(Path(__file__).parents[1] / 'pages' / '1_指数监控.py').read_text()
    source=source.replace('from datetime import datetime\n', '''from datetime import datetime as _datetime
class datetime(_datetime):
    @classmethod
    def now(cls, tz=None):
        return _datetime(2026,10,1,23,10,tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(tz)
''',1)
    names=['标普500','纳斯达克综合','纳斯达克100','VIX恐慌指数','沪深300']
    summary=pd.DataFrame({'指数':names,'代码':['a','b','c','d','e'],'日期':['2026-09-30']*5,
                          '收盘价':[90.]*5,'前收盘价':[89.]*5,'当日涨跌幅(%)':[1.]*5,'偏离率(%)':[0.]*5})
    history=pd.DataFrame({'日期':['2026-09-30'],'指数':['标普500'],'收盘价':[90.]})
    meta={'last_trade_date':'2026-09-30','last_update_time':'2026-10-01 15:10:00'}
    us._NEXT_ATTEMPT.clear();us._INFLIGHT.clear();us._ERRORS.clear();ir._RUNTIME_QUOTE_CACHE.clear()
    import streamlit as st
    real_markdown=st.markdown
    with patch('streamlit.markdown',wraps=real_markdown) as markdown:
        def fetch(**kw):
            assert kw['force_index_names']==us.US_AUTO_REFRESH_INDEX_NAMES
            assert any('index-card-value">90.00' in str(c.args[0]) for c in markdown.call_args_list)
            return prices(kw['now'],kw['force_index_names'])
        with (
            patch('core.db.init_db'),
            patch('core.cache.load_dataset',return_value=(history,meta)),
            patch('core.cache.list_datasets',return_value=pd.DataFrame()),
            patch('services.index_ma20.build_summary',return_value=summary),
            patch('services.index_ma20.sanitize_index_report_market_dates',side_effect=lambda x:x),
            patch('services.update_tasks.enrich_index_report_indicators',side_effect=lambda x:x),
            patch('services.update_tasks.trim_index_report',side_effect=lambda x,**kw:x),
            patch.object(ir,'load_index_formal_trade_dates',return_value={}),
            patch.object(ir,'find_pending_post_close_index_names',return_value=set()),
            patch.object(ir,'fetch_realtime_index_quotes',side_effect=fetch) as quote_fetch,
            patch('services.update_tasks.run_index_ma20_update') as formal_update,
            patch('core.cache.save_dataset') as save,
        ):
            first=AppTest.from_string(source,default_timeout=30).run()
            assert not first.exception
            assert quote_fetch.call_count==1
            cards=[m.value for m in first.markdown if 'index-card-single ' in m.value]
            assert len(cards)==5
            assert sum('index-card-value">100.00' in m for m in cards)==3
            first.run()
            assert not first.exception and quote_fetch.call_count==1
            second=AppTest.from_string(source,default_timeout=30).run()
            assert not second.exception and quote_fetch.call_count==1
            formal_update.assert_not_called();save.assert_not_called()
    us._NEXT_ATTEMPT.clear();us._INFLIGHT.clear();us._ERRORS.clear();ir._RUNTIME_QUOTE_CACHE.clear()


def ny(value):return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('America/New_York'))
def prices(now,names):return {name:{'price':100.,'quote_time':now,'source':'测试'} for name in names}

@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    # Compatibility facades propagate patch targets into their implementation
    # modules. Restore those bindings after the isolated page test as well.
    from services import index_ma20, update_tasks
    saved_bindings = [
        (module, name, module.__dict__[name])
        for facade in (index_ma20, update_tasks)
        for module in facade._MODULES
        for name in facade._SYNC_NAMES
        if name in module.__dict__
    ]
    monkeypatch.setattr(us,'US_AUTO_REFRESH_INDEX_NAMES',frozenset({'标普500','纳斯达克综合','纳斯达克100'}))
    us._NEXT_ATTEMPT.clear();us._INFLIGHT.clear();us._ERRORS.clear()
    ir._RUNTIME_QUOTE_CACHE.clear()
    yield
    for module, name, value in saved_bindings:
        module.__dict__[name] = value
    us._NEXT_ATTEMPT.clear();us._INFLIGHT.clear();us._ERRORS.clear()
    ir._RUNTIME_QUOTE_CACHE.clear()

@pytest.mark.parametrize('value',[
 '2026-10-01 09:29','2026-10-01 16:00','2026-10-03 11:00',
 '2026-12-25 11:00','2026-11-27 13:00','2027-01-01 11:00'])
def test_no_quotes_outside_cash_session(value):
    with patch.object(ir,'fetch_realtime_index_quotes') as fetch:
        assert not us.refresh_us_index_quotes(now=ny(value)).attempted_names
    fetch.assert_not_called()

def test_only_verified_us_names_and_cross_browser_two_minute_gate():
    now=ny('2026-10-01 11:00')
    def fetch(**kw):return prices(kw['now'],kw['force_index_names'])
    with patch.object(ir,'fetch_realtime_index_quotes',side_effect=fetch) as source, patch.object(ir,'save_dataset') as save:
        first=us.refresh_us_index_quotes(now=now)
        assert first.attempted_names==us.US_AUTO_REFRESH_INDEX_NAMES
        assert 'VIX恐慌指数' not in first.attempted_names
        for seconds in (0,30,119):assert not us.refresh_us_index_quotes(now=now+timedelta(seconds=seconds)).attempted_names
        assert us.refresh_us_index_quotes(now=now+timedelta(seconds=120)).attempted_names==first.attempted_names
        assert source.call_count==2
        save.assert_not_called()
    assert ir.load_runtime_realtime_quotes()['标普500']['price']==100.

def test_partial_failure_retains_good_quote_and_retries_only_after_ten_minutes():
    now=ny('2026-10-01 11:00')
    old=prices(now-timedelta(minutes=10),{'标普500'})
    ir.remember_runtime_realtime_quotes(old)
    def fetch(**kw):
        return prices(kw['now'],kw['force_index_names']-{'标普500'})
    with patch.object(ir,'fetch_realtime_index_quotes',side_effect=fetch) as source:
        first=us.refresh_us_index_quotes(now=now)
        assert set(first.errors)=={'标普500'}
        assert ir.load_runtime_realtime_quotes()['标普500']==old['标普500']
        us.refresh_us_index_quotes(now=now+timedelta(seconds=120))
        assert source.call_args.kwargs['force_index_names']=={'纳斯达克综合','纳斯达克100'}
        us.refresh_us_index_quotes(now=now+timedelta(seconds=599))
        assert '标普500' not in source.call_args.kwargs['force_index_names']
        us.refresh_us_index_quotes(now=now+timedelta(seconds=600))
        assert '标普500' in source.call_args.kwargs['force_index_names']

@pytest.mark.parametrize('price,age',[(0,0),(float('nan'),0),(float('inf'),0),(100,181),(100,900),(100,-6),(100,86400)])
def test_bad_prices_or_old_future_and_previous_day_stamps_fail(price,age):
    now=ny('2026-10-01 11:00')
    assert not us.current_us_quote({'price':price,'quote_time':now-timedelta(seconds=age)},now)

def test_valid_manual_quote_is_reused_without_new_network_call():
    now=ny('2026-10-01 11:00')
    ir.remember_runtime_realtime_quotes(prices(now-timedelta(seconds=10),us.US_AUTO_REFRESH_INDEX_NAMES))
    with patch.object(ir,'fetch_realtime_index_quotes') as source:
        assert not us.refresh_us_index_quotes(now=now).attempted_names
        source.assert_not_called()


def test_successful_manual_refresh_clears_failure_and_satisfies_automatic_slot():
    now=ny('2026-10-01 11:00')
    with patch.object(ir,'fetch_realtime_index_quotes',return_value={}):
        assert us.refresh_us_index_quotes(now=now).errors
    refreshed=now+timedelta(seconds=30)
    good=prices(refreshed,us.US_AUTO_REFRESH_INDEX_NAMES)
    ir.remember_runtime_realtime_quotes(good)
    us.remember_us_manual_refresh(good,now=refreshed)
    with patch.object(ir,'fetch_realtime_index_quotes') as fetch:
        result=us.refresh_us_index_quotes(now=refreshed+timedelta(seconds=119))
        assert not result.errors and not result.attempted_names
        fetch.assert_not_called()

def test_concurrent_browser_cannot_duplicate_inflight_requests():
    now=ny('2026-10-01 11:00')
    entered=Event();release=Event();results=[]
    def fetch(**kw):
        entered.set();assert release.wait(5)
        return prices(kw['now'],kw['force_index_names'])
    with patch.object(ir,'fetch_realtime_index_quotes',side_effect=fetch) as source:
        thread=Thread(target=lambda:results.append(us.refresh_us_index_quotes(now=now)))
        thread.start()
        try:
            assert entered.wait(5)
            assert not us.refresh_us_index_quotes(now=now+timedelta(seconds=120)).attempted_names
            assert source.call_count==1
        finally:
            release.set();thread.join(5)
        assert not thread.is_alive()
        assert len(results)==1 and results[0].quotes

def test_stale_response_remains_retryable_and_does_not_overwrite_latest_quote():
    now=ny('2026-10-01 11:00')
    with patch.object(ir,'fetch_realtime_index_quotes',return_value=prices(now-timedelta(minutes=15),us.US_AUTO_REFRESH_INDEX_NAMES)):
        result=us.refresh_us_index_quotes(now=now)
    assert len(result.errors)==3 and not result.quotes
    assert not ir.load_runtime_realtime_quotes()

def test_china_midnight_does_not_reset_new_york_session_gate():
    now=ny('2026-10-01 11:59:30')
    def fetch(**kw):return prices(kw['now'],kw['force_index_names'])
    with patch.object(ir,'fetch_realtime_index_quotes',side_effect=fetch) as source:
        us.refresh_us_index_quotes(now=now)
        us.refresh_us_index_quotes(now=(now+timedelta(seconds=60)).astimezone(ZoneInfo('Asia/Shanghai')))
        assert source.call_count==1

@pytest.mark.parametrize('utc_value', ['2026-10-01 13:30','2026-11-12 14:30'])
def test_summer_and_winter_open_hours_use_market_calendar(utc_value):
    now=datetime.fromisoformat(utc_value).replace(tzinfo=ZoneInfo('UTC'))
    with patch.object(ir,'fetch_realtime_index_quotes',side_effect=lambda **kw:prices(kw['now'],kw['force_index_names'])):
        assert us.refresh_us_index_quotes(now=now).attempted_names==us.US_AUTO_REFRESH_INDEX_NAMES
