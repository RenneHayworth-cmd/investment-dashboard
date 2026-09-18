from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from services import futures_spread as source

ENV = {"INVESTMENT_DASHBOARD_ALERT_TICKFLOW_TIMEOUT_SECONDS": "12"}


def test_production_daily_never_enters_unbounded_akshare():
    with patch.dict("os.environ", ENV), patch(
        "akshare.futures_zh_daily_sina", side_effect=AssertionError("unbounded")
    ), patch.object(source, "_fetch_futures_daily_from_sina_direct", side_effect=TimeoutError):
        with pytest.raises(TimeoutError):
            source.fetch_futures_daily_from_akshare("I2701")


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
