from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from services import futures_spread as source

ENV = {"INVESTMENT_DASHBOARD_ALERT_TICKFLOW_TIMEOUT_SECONDS": "12"}


def test_production_daily_tries_bounded_backups_and_reports_all_errors():
    with patch.dict("os.environ", ENV), patch(
        "akshare.futures_zh_daily_sina", side_effect=TimeoutError("新浪超时")
    ) as sina, patch("akshare.futures_hist_table_em", return_value=pd.DataFrame({"合约代码": ["i2701"]})), \
         patch("akshare.futures_hist_em", side_effect=TimeoutError("东方财富超时")) as em, \
         patch.object(source, "_fetch_futures_daily_from_sina_direct", side_effect=TimeoutError("直连超时")):
        with pytest.raises(RuntimeError, match="直连超时.*新浪超时.*东方财富超时"):
            source.fetch_futures_daily_from_akshare("I2701")
    sina.assert_called_once()
    em.assert_called_once()


def test_production_spot_timeout_retains_history():
    history = pd.DataFrame({"date": pd.to_datetime(["2026-09-17"]), "close": [800.]})
    with patch.dict("os.environ", ENV), patch(
        "akshare.futures_zh_spot", side_effect=AssertionError("unbounded")
    ), patch.object(source, "_fetch_futures_spot_from_sina_direct", side_effect=TimeoutError):
        result = source.append_futures_spot_row(
            history, "I2701", replace_current_day=True,
            market_now=datetime(2026, 9, 18, 10, tzinfo=ZoneInfo("Asia/Shanghai")))
    pd.testing.assert_frame_equal(result, history)


def test_futures_tickflow_inherits_production_timeout():
    with patch.dict("os.environ", {**ENV, "INVESTMENT_DASHBOARD_ALERT_TICKFLOW_MAX_RETRIES": "0"}), patch("tickflow.TickFlow") as client:
        client.return_value.klines.get.return_value = pd.DataFrame({
            "date": ["2026-09-17"], "close": [800.]})
        source.fetch_futures_daily_from_tickflow("I2701", api_key="test-key")
        client.assert_called_once_with(api_key="test-key", timeout=12., max_retries=0)
