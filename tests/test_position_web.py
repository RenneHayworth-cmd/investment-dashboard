"""HTTP parity checks use real strategy services and deterministic formal prices."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo
import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from services import position_models as models, position_timing as timing
from services import position_performance as performance, position_runtime as runtime
from web.backend.adapter import build_dashboard, json_value
from web.backend.app import create_app
from web.backend.coordinator import Coordinator

NOW = datetime(2026, 8, 10, 14, 52, tzinfo=ZoneInfo('Asia/Shanghai'))


def items():
    dates = pd.bdate_range(end='2026-08-07', periods=60)
    output = []
    for i, code in enumerate(models.DEFAULT_ETF_CODES):
        prices = [100.] * len(dates)
        prices[-3:] = [120., 120., 90.] if i % 2 else [90., 120., 120.]
        frame = pd.DataFrame({'date': dates, 'price': prices})
        strategy = models.ETF_TIMING_STRATEGIES.get(code)
        metrics = {'最新价': prices[-1], '日涨跌(%)': -25. if i % 2 else 0.}
        if strategy:
            metrics.update(timing.calculate_etf_timing_snapshot(frame, ma_period=strategy[0], threshold_pct=strategy[1]))
        output.append(models.PositionItem('ETF', code, models.ETF_DISPLAY_NAMES[code], '缓存',
            latest_date='2026-08-07', dataframe=frame, metrics=metrics))
    return output


@pytest.fixture(autouse=True)
def no_index_cache():
    with patch.object(timing, '_load_position_index_timing_history', return_value=None):
        yield


def payload(formal=None, quotes=None, now=NOW):
    return build_dashboard(items() if formal is None else formal, [], quotes or {}, {}, now)


def client_for(data):
    state = Coordinator()
    state.payload = data
    return TestClient(create_app(state, start_worker=False)), state


def test_health_and_no_debug_endpoints():
    client, _ = client_for(payload())
    assert client.get('/api/health').json() == {'status': 'ok', 'ready': True, 'refreshing': False}
    assert client.get('/docs').status_code == 404
    assert client.get('/openapi.json').status_code == 404


def test_etf_serialization_parity():
    formal = items()
    client, _ = client_for(payload(formal))
    response = client.get('/api/timing/etf').json()
    assert response['formal'] == json_value(timing.build_etf_timing_table(formal))
    assert all(len(row['代码']) == 6 for row in response['formal'])
    assert response['preview'] == []


@pytest.mark.parametrize('section,attribute', [('summary','summary'),('positions','positions'),('performance','daily'),('trades','trades')])
def test_strategy_http_parity(section, attribute):
    formal = items()
    expected = performance.build_position_timing_performance(formal, market_now=NOW)
    assert not expected.errors
    assert not expected.positions.empty
    assert not expected.trades.empty
    client, _ = client_for(payload(formal))
    assert client.get('/api/strategy/' + section).json()['data'] == json_value(getattr(expected, attribute))
    assert expected.daily.iloc[0]['账户资产'] == 500_000
    assert expected.daily.iloc[0]['净值'] == 1
    assert expected.daily_by_symbol['当日盈亏'].sum() == pytest.approx(expected.daily.iloc[-1]['每日盈亏'], abs=0.02)


def test_preview_does_not_change_formal_guidance_or_strategy():
    formal = items()
    before = [item.dataframe.copy(deep=True) for item in formal]
    quotes = {item.code: {'symbol':item.code, 'price':150., 'previous_close':100., 'quote_time': NOW} for item in formal}
    result = payload(formal, quotes)
    baseline = payload(formal)
    assert result['etf_preview'] != result['etf_formal']
    assert result['preview_codes']
    assert result['guidance'] == baseline['guidance']
    assert result['strategy'] == baseline['strategy']
    assert result['strategy_live']['complete']
    assert result['strategy_live'] != baseline['strategy_live']
    expected_live = performance.build_position_timing_intraday_valuation(
        performance.build_position_timing_performance(formal, market_now=NOW), quotes, market_now=NOW)
    assert result['strategy_live'] == json_value(expected_live)
    assert result['trade_preview'] == json_value(performance.build_position_timing_trade_preview(formal, quotes, market_now=NOW))
    for old, item in zip(before, formal):
        pd.testing.assert_frame_equal(old, item.dataframe)


def test_after_close_retains_quote_until_formal_arrives():
    after = NOW.replace(hour=15, minute=6)
    formal = items()
    quote = {'159501': {'symbol':'159501.SZ','price':130.,'quote_time':NOW}}
    pending = payload(formal, quote, after)
    assert '159501' in pending['preview_codes']
    assert pending['formal_dates']['159501'] == '2026-08-07'
    assert '159501' in pending['missing_formal_codes']
    for item in formal:
        item.dataframe = pd.concat([item.dataframe, pd.DataFrame({'date':[pd.Timestamp('2026-08-10')],'price':[130.]})],ignore_index=True)
        item.latest_date = '2026-08-10'
    confirmed = payload(formal, quote, after)
    assert confirmed['preview_codes'] == []
    assert confirmed['quote_time'] == ''
    assert pending['strategy_live']['mode'] == 'pending_close'
    assert confirmed['strategy_live']['mode'] == 'formal'
    assert confirmed['strategy_live']['daily_pnl'] is None
    assert confirmed['strategy']['daily'][-1]['日期'].startswith('2026-08-10')


def test_live_does_not_execute_preview_actions():
    formal = items()
    quotes = {item.code: {'price': 130., 'quote_time': NOW} for item in formal}
    baseline = payload(formal, quotes)
    fabricated_preview = performance.PositionTimingTradePreviewResult(
        actions=pd.DataFrame([{'代码': '159501', '操作': '买入', '数量': 9999999}]))
    with patch.object(performance, 'build_position_timing_trade_preview', return_value=fabricated_preview):
        changed = payload(formal, quotes)
    assert baseline['trade_preview'] != changed['trade_preview']
    assert baseline['strategy_live'] == changed['strategy_live']
    assert baseline['strategy'] == changed['strategy']


def test_partial_formal_confirmation_keeps_all_valuation_quotes():
    formal = items()
    quotes = {item.code: {'price': 130., 'quote_time': NOW} for item in formal}
    before = payload(formal, quotes)
    held = before['strategy']['positions'][0]['代码']
    item = next(item for item in formal if item.code == held)
    item.dataframe = pd.concat([item.dataframe, pd.DataFrame({'date': [pd.Timestamp('2026-08-10')], 'price': [130.]})], ignore_index=True)
    item.latest_date = '2026-08-10'
    after = payload(formal, quotes, NOW.replace(hour=15, minute=6))
    assert held not in after['preview_codes']
    assert after['strategy_live']['complete']
    assert after['strategy_live']['daily_pnl'] == before['strategy_live']['daily_pnl']
    assert after['strategy_live']['mode'] == 'pending_close'


def test_runtime_snapshot_update_changes_only_live_valuation_without_fetch():
    state = Coordinator(clock=lambda: NOW)
    state.items = items()
    shared = {item.code: {'price': 130., 'quote_time': NOW} for item in state.items}
    with patch('web.backend.coordinator.list_datasets', return_value=pd.DataFrame()), \
         patch.object(runtime, 'load_runtime_etf_quotes', side_effect=lambda **kw: shared), \
         patch.object(runtime, 'fetch_tickflow_etf_quotes', side_effect=AssertionError('new fetch')), \
         patch('core.cache.save_dataset', side_effect=AssertionError('persisted preview')):
        state._publish(NOW)
        first = state.payload
        for quote in shared.values():
            quote['price'] = 140.
        state._publish(NOW.replace(second=30))
        second = state.payload
    assert first['strategy'] == second['strategy']
    assert first['strategy_live']['daily_pnl'] != second['strategy_live']['daily_pnl']


def test_missing_data_is_explicit_and_not_zero():
    client, _ = client_for(payload([]))
    result = client.get('/api/dashboard').json()
    assert result['strategy']['summary'] == {}
    assert result['strategy']['errors']
    assert result['strategy']['daily'] == []
    assert result['index_formal'][0]['最新收盘'] is None


def test_invalid_adjustment_suppresses_simulation():
    formal = items()
    next(item for item in formal if item.code == '159501').formal_history_valid = False
    result = payload(formal)
    assert result['strategy']['errors']
    assert result['strategy']['positions'] == []


def test_json_nulls_dates_numpy_and_secret(monkeypatch):
    monkeypatch.setenv('TICKFLOW_API_KEY','test-secret-never-return')
    value = json_value({'x':np.float64('nan'), 'y':pd.NA, 'z':float('inf'), 'n':np.int64(3), 'date':NOW, 'error':'test-secret-never-return https://source.test/?token=hidden'})
    encoded = json.dumps(value, allow_nan=False)
    assert value['x'] is None and value['y'] is None and value['z'] is None
    assert value['date'] == '2026-08-10 14:52:00'
    assert 'test-secret' not in encoded and 'token=' not in encoded


def test_refresh_coalesces_devices_and_protects_origin(monkeypatch):
    client, state = client_for(payload())
    assert client.post('/api/refresh').status_code == 403
    monkeypatch.setenv('WEB_ORIGIN','https://position.test')
    assert client.post('/api/refresh',headers={'X-Position-Client':'web','Origin':'https://evil.test'}).status_code == 403
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(lambda _: state.request_refresh(), range(20)))
    assert sum(accepted) == 1
    assert state.wake.is_set()
    assert client.post('/api/refresh',headers={'X-Position-Client':'web'}).json()['accepted'] is False


def test_get_reads_snapshot_without_recomputing_or_fetching():
    client, _ = client_for(payload())
    with patch.object(performance, 'build_position_timing_performance', side_effect=AssertionError('GET recalculated')), patch.object(runtime, 'fetch_tickflow_etf_quotes', side_effect=AssertionError('GET fetched')):
        for _ in range(4):
            assert client.get('/api/dashboard').status_code == 200
            assert client.get('/api/strategy/summary').status_code == 200


def test_retry_target_change_and_cooldown():
    state = Coordinator()
    assert state._due('159501','2026-08-07')
    assert not state._due('159501','2026-08-07')
    assert state._due('159501','2026-08-10')


def test_shared_runtime_reuses_quote_batch_and_lunch_success():
    with runtime._RUNTIME_ETF_QUOTE_CACHE_LOCK:
        old_quotes = dict(runtime._RUNTIME_ETF_QUOTE_CACHE)
        old_state = dict(runtime._RUNTIME_ETF_QUOTE_FETCH_STATE)
        runtime._RUNTIME_ETF_QUOTE_CACHE.clear()
        runtime._RUNTIME_ETF_QUOTE_FETCH_STATE.clear()
    try:
        quotes = {'159501': {'symbol':'159501.SZ','price':110.,'quote_time':NOW}}
        with patch.object(runtime,'fetch_tickflow_etf_quotes',return_value=quotes) as fetch:
            runtime.refresh_runtime_etf_quotes(['159501'],api_key='test',market_now=NOW)
            runtime.refresh_runtime_etf_quotes(['159501'],api_key='test',market_now=NOW.replace(second=30))
            assert fetch.call_count == 1
            runtime.refresh_runtime_etf_quotes(['159501'],api_key='test',market_now=NOW.replace(minute=54))
            assert fetch.call_count == 2
            lunch = NOW.replace(hour=11,minute=31)
            runtime.refresh_runtime_etf_quotes(['159501'],api_key='test',market_now=lunch)
            runtime.refresh_runtime_etf_quotes(['159501'],api_key='test',market_now=lunch.replace(hour=12,minute=30))
            assert fetch.call_count == 3
    finally:
        with runtime._RUNTIME_ETF_QUOTE_CACHE_LOCK:
            runtime._RUNTIME_ETF_QUOTE_CACHE.clear(); runtime._RUNTIME_ETF_QUOTE_CACHE.update(old_quotes)
            runtime._RUNTIME_ETF_QUOTE_FETCH_STATE.clear(); runtime._RUNTIME_ETF_QUOTE_FETCH_STATE.update(old_state)


def test_manual_refresh_forces_one_current_band_quote_batch():
    with runtime._RUNTIME_ETF_QUOTE_CACHE_LOCK:
        old_state = dict(runtime._RUNTIME_ETF_QUOTE_FETCH_STATE)
        runtime._RUNTIME_ETF_QUOTE_FETCH_STATE.clear()
    try:
        quotes = {'159501': {'symbol': '159501.SZ', 'price': 110., 'quote_time': NOW}}
        with patch.object(runtime, 'fetch_tickflow_etf_quotes', return_value=quotes) as fetch:
            runtime.refresh_runtime_etf_quotes(['159501'], api_key='test', market_now=NOW)
            runtime.refresh_runtime_etf_quotes(['159501'], api_key='test', market_now=NOW.replace(second=30), force=True)
            assert fetch.call_count == 2
    finally:
        with runtime._RUNTIME_ETF_QUOTE_CACHE_LOCK:
            runtime._RUNTIME_ETF_QUOTE_FETCH_STATE.clear()
            runtime._RUNTIME_ETF_QUOTE_FETCH_STATE.update(old_state)


def test_initialization_is_503_not_fabricated_data():
    client = TestClient(create_app(Coordinator(), start_worker=False))
    assert client.get('/api/dashboard').status_code == 503
    assert client.get('/api/health').json()['ready'] is False


def test_formal_spread_refresh_never_rewrites_last_cached_date():
    from services import position_derivatives as derivative
    from services.futures_spread import SPREAD_CALCULATION_VERSION
    cached = pd.DataFrame({'date':pd.to_datetime(['2026-08-06','2026-08-07']),
        'I2701_close':[800.,805.], 'I2705_close':[780.,782.],
        'spread_I2701_vs_I2705':[20.,23.], 'spread_I2701_vs_I2705_pct':[2.5,2.8571],
        '_calculation_version':SPREAD_CALCULATION_VERSION})
    changed = cached.iloc[[-1]].copy()
    changed['I2701_close'] = 900.
    changed['spread_I2701_vs_I2705'] = 118.
    with patch.object(derivative,'_load_dataset_if_ready',return_value=(cached,{})), \
         patch.object(derivative,'_spread_cache_matches_contracts',return_value=True), \
         patch.object(derivative,'fetch_contracts',return_value=({},[])), \
         patch.object(derivative,'calculate_spreads',return_value=changed), \
         patch.object(derivative,'save_dataset') as save:
        result=derivative.load_or_fetch_spread(['I2701','I2705'],force_refresh=True,market_now=NOW)
        assert result.dataframe.iloc[-1]['I2701_close'] == 805.
        assert save.call_args.kwargs['df'].iloc[-1]['spread_I2701_vs_I2705'] == 23.


def test_close_audit_never_persists_fetch_arguments():
    from services import position_close_audit as audit
    item=items()[0]
    with patch.object(audit,'start_job',return_value=1),patch.object(audit,'finish_job') as finish:
        result=audit.fetch_audited_close('512890',fetcher=lambda code,**kw:item,
            target_date=NOW.date(),api_key='do-not-store-secret')
        assert result is item
        assert 'do-not-store-secret' not in str(finish.call_args)


def test_cached_formal_errors_survive_local_reload():
    state=Coordinator(clock=lambda:NOW)
    state.formal_errors['159501']=(NOW.date(),'TickFlow：请求限流','TickFlow')
    # The attempt target is the last completed ETF day, not the calendar day.
    from services.position_sessions import latest_final_etf_trade_date
    state.formal_errors['159501']=(latest_final_etf_trade_date(NOW),'TickFlow：请求限流','TickFlow')
    missing=models._missing_item('ETF','159501')
    from services import position_market, position_derivatives
    with patch.object(position_market,'load_or_fetch_etf',return_value=missing), \
         patch.object(position_derivatives,'load_or_fetch_futures_contract',return_value=models._missing_item('期货','I2701')), \
         patch.object(position_derivatives,'load_or_fetch_spread',return_value=models._missing_item('期货价差','I2701 - I2705')):
        state._load_local(NOW)
    assert state.items[0].error == 'TickFlow：请求限流'


def test_coordinator_does_not_fetch_current_index_history():
    from services import position_market, position_derivatives
    state=Coordinator(api_key='test',clock=lambda:NOW.replace(hour=8))
    formal=items()
    def local(now):
        state.items=formal
        state.derivatives=[]
    with patch.object(state,'_load_local',side_effect=local),patch.object(state,'_publish'), \
         patch('web.backend.coordinator.missing_recent_market_trade_dates',return_value=[]), \
         patch('web.backend.coordinator.run_index_ma20_update') as update, \
         patch.object(position_derivatives,'load_or_fetch_futures_contract',return_value=models._missing_item('期货','I2701')), \
         patch.object(position_derivatives,'load_or_fetch_spread',return_value=models._missing_item('期货价差','I2701 - I2705')), \
         patch.object(position_market,'load_or_fetch_etf') as fetch:
        state._cycle()
    update.assert_not_called()
    fetch.assert_not_called()


def test_snapshot_reuses_calculation_until_data_or_session_changes():
    state=Coordinator(clock=lambda:NOW)
    state.items=items()
    with patch('web.backend.coordinator.list_datasets',return_value=pd.DataFrame()), \
         patch.object(runtime,'load_runtime_etf_quotes',return_value={}), \
         patch('web.backend.coordinator.build_dashboard',return_value={'test':True}) as build:
        state._publish(NOW)
        state._publish(NOW.replace(second=30))
        assert build.call_count == 1
        state.items[0].dataframe.loc[0,'price']=101.
        state._publish(NOW)
        assert build.call_count == 2
        state._publish(NOW.replace(hour=15,minute=6))
        assert build.call_count == 3
