from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from services import futures_live_realtime as realtime

TZ = ZoneInfo("Asia/Shanghai")


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


@pytest.mark.parametrize(
    ("contract", "moment", "expected"),
    [
        ("I2701", "2026-09-30 09:05", date(2026, 9, 30)),
        ("I2701", "2026-09-30 10:20", None),  # 商品期货 10:15-10:30 休息
        ("I2701", "2026-09-29 21:30", date(2026, 9, 30)),  # 夜盘归属下一交易日
        ("I2701", "2026-09-30 21:30", None),  # 国庆节前夜盘停盘
        ("I2701", "2026-10-09 21:30", date(2026, 10, 12)),  # 周五夜盘归属周一
        ("AU2612", "2026-10-10 01:30", date(2026, 10, 12)),  # 黄金夜盘延续到次日凌晨
        ("IM2612", "2026-09-30 09:05", None),  # 中金所 9:30 开盘
        ("MO2612-P-6000", "2026-09-30 13:10", date(2026, 9, 30)),
        ("I2701", "2026-10-03 10:00", None),  # 节假日
    ],
)
def test_session_trade_date(contract, moment, expected):
    assert realtime.session_trade_date(contract, _at(moment)) == expected


def test_refresh_due_respects_interval_and_failed_retry():
    now = _at("2026-09-30 10:00")
    assert realtime.quote_refresh_due(["I2701"], now, {}) is True
    recent = {"fetched_at": "2026-09-30T09:59:00", "failures": {}}
    assert realtime.quote_refresh_due(["I2701"], now, recent) is False
    stale = {"fetched_at": "2026-09-30T09:57:00", "failures": {}}
    assert realtime.quote_refresh_due(["I2701"], now, stale) is True
    failed = {"fetched_at": "2026-09-30T09:57:00", "failures": {"I2701": "x"}, "succeeded": 0}
    assert realtime.quote_refresh_due(["I2701"], now, failed) is False
    assert realtime.quote_refresh_due(["I2701"], _at("2026-09-30 10:07"), failed) is True
    partial = {"fetched_at": "2026-09-30T09:57:00", "failures": {"I2705": "x"}, "succeeded": 1}
    assert realtime.quote_refresh_due(["I2701"], now, partial) is True
    assert realtime.quote_refresh_due(["I2701"], _at("2026-09-30 20:00"), {}) is False


def test_preview_uses_latest_settlement_as_base():
    positions = pd.DataFrame(
        [
            {"asset_type": "期货", "contract": "I2701", "side": "多", "estimated_quantity": 7, "multiplier": 100,
             "valuation_price": 704.5, "valuation_date": "2026-09-30", "average_price": 714.64},
            {"asset_type": "期货", "contract": "I2705", "side": "空", "estimated_quantity": 3, "multiplier": 100,
             "valuation_price": 694.5, "valuation_date": "2026-09-30", "average_price": 701.83},
        ]
    )
    quotes = {
        "I2701": {"price": 710.0, "quote_time": "101500", "source": "新浪期货实时"},
        "I2705": {"price": 700.0, "quote_time": "101500", "source": "新浪期货实时"},
    }

    preview = realtime.build_futures_intraday_preview(positions, quotes).set_index("contract")

    assert preview.loc["I2701", "intraday_pnl"] == pytest.approx((710 - 704.5) * 7 * 100)
    assert preview.loc["I2705", "intraday_pnl"] == pytest.approx(-(700 - 694.5) * 3 * 100)
    assert preview.loc["I2701", "floating_pnl"] == pytest.approx((710 - 714.64) * 7 * 100)


def test_quotes_from_another_trading_day_are_rejected(monkeypatch):
    positions = pd.DataFrame(
        [{"asset_type": "期货", "contract": "I2701", "estimated_quantity": 7}]
    )
    monkeypatch.setattr(
        realtime,
        "_fetch_futures_quote",
        lambda contract: {"price": 702.5, "quote_date": pd.Timestamp("2026-09-29"), "quote_time": "150000",
                          "source": "新浪期货实时"},
    )

    quotes, failures = realtime.fetch_futures_live_quotes(positions, _at("2026-09-30 10:00"))

    assert quotes == {}
    assert "不是当前交易日" in failures["I2701"]


def test_quotes_outside_sessions_are_not_requested(monkeypatch):
    positions = pd.DataFrame([{"asset_type": "期货", "contract": "I2701", "estimated_quantity": 7}])
    monkeypatch.setattr(realtime, "_fetch_futures_quote", lambda contract: pytest.fail("不应联网"))

    assert realtime.fetch_futures_live_quotes(positions, _at("2026-10-03 10:00")) == ({}, {})


def test_intraday_preview_section_renders_transient_quotes():
    from unittest.mock import patch

    from streamlit.testing.v1 import AppTest

    def page():
        import pandas as pd

        from components.futures_live.realtime import render_intraday_preview

        settlement = pd.DataFrame(
            [{"date": "2026-09-30", "status": "完整", "economic_equity": 175_449.12}]
        )
        render_intraday_preview(settlement)

    positions = pd.DataFrame(
        [{"asset_type": "期货", "contract": "I2701", "side": "多", "estimated_quantity": 7, "multiplier": 100,
          "valuation_price": 704.5, "valuation_date": "2026-09-30", "average_price": 714.64}]
    )
    quotes = {"I2701": {"price": 710.0, "quote_time": "101500", "source": "新浪期货实时",
                        "trade_date": date(2026, 10, 9), "fetched_at": "2026-10-09T10:15:00"}}
    with (
        patch("services.futures_live_trading.build_current_position_pnl", return_value=positions),
        patch("components.futures_live.realtime.quote_refresh_due", return_value=True),
        patch("components.futures_live.realtime.fetch_futures_live_quotes", return_value=(quotes, {})),
        patch("components.futures_live.realtime.session_trade_date", return_value=date(2026, 10, 9)),
    ):
        app = AppTest.from_function(page, default_timeout=30).run()

    assert list(app.exception) == []
    assert "盘中估算" in [item.value for item in app.subheader]
    assert app.metric[0].value == "+3,850.00"
    assert app.metric[1].value == "179,299.12"
