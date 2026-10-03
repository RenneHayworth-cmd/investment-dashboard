from pathlib import Path
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from services.home_overview import (
    AccountSeries,
    _account_frame,
    combine_account_series,
    combined_start_date,
    futures_account_series,
    load_pre_ledger_pnl,
    microcap_account_series,
    save_pre_ledger_pnl,
)


def _account(key, rows, **kwargs):
    dates, pnl, base = zip(*rows)
    return AccountSeries(key, _account_frame(list(dates), list(pnl), list(base)), **kwargs)


def test_combined_sums_amounts_and_bases_then_compounds():
    etf = _account("etf", [("2026-08-04", 0.0, 1000.0), ("2026-08-05", 10.0, 1000.0), ("2026-08-06", -20.0, 1010.0)])
    futures = _account("futures", [("2026-08-05", 30.0, 500.0), ("2026-08-06", 5.0, 530.0)])

    combined = combine_account_series([etf, futures], start=combined_start_date([etf, futures]))

    assert combined["pnl_amount"].tolist() == [0.0, 40.0, -15.0]
    assert combined["return_base"].tolist() == [1000.0, 1500.0, 1540.0]
    assert abs(combined["nav"].iloc[-1] - (1 + 40 / 1500) * (1 - 15 / 1540)) < 1e-12
    assert combined["cumulative_pnl"].iloc[-1] == 25.0


def test_start_is_etf_opening_and_its_day_is_only_a_baseline():
    etf = _account("etf", [("2026-08-04", 0.0, 1000.0), ("2026-08-05", 10.0, 1000.0)])
    futures = _account("futures", [("2026-07-31", -50.0, 500.0), ("2026-08-04", 650.0, 500.0), ("2026-08-05", 1.0, 1150.0)])

    start = combined_start_date([etf, futures])
    combined = combine_account_series([etf, futures], start=start)

    assert start == pd.Timestamp("2026-08-04")
    assert combined["date"].min() == start
    assert combined.iloc[0]["pnl_amount"] == 0.0
    assert combined["cumulative_pnl"].iloc[-1] == 11.0


def test_combined_stops_at_the_earliest_latest_valuation():
    etf = _account("etf", [("2026-09-29", 0.0, 1000.0), ("2026-09-30", 10.0, 1000.0)])
    microcap = _account("microcap", [("2026-09-29", 1.0, 100.0)])

    combined = combine_account_series([etf, microcap])

    assert combined["date"].max() == pd.Timestamp("2026-09-29")


def test_microcap_base_is_assets_minus_pnl():
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-09-23", "2026-09-24"]),
            "total_assets": [219_000.0, 221_000.0],
            "pnl_amount": [-1_000.0, 2_000.0],
            "total_pnl": [-1_000.0, 1_000.0],
        }
    )

    series = microcap_account_series(daily)

    assert series.daily["return_base"].tolist() == [220_000.0, 219_000.0]
    assert series.total_assets == 221_000.0
    assert series.cumulative_pnl == 1_000.0


def test_futures_uses_valued_settlement_days_and_keeps_confirmation():
    daily = pd.DataFrame(
        {
            "date": ["2026-08-17", "2026-08-18", "2026-08-19"],
            "status": ["手工估算", "完整", "数据不完整"],
            "daily_pnl": [870.0, 30.0, None],
            "return_base": [300_000.0, 300_870.0, None],
            "economic_equity": [300_870.0, 300_900.0, None],
            "net_pnl": [870.0, 900.0, None],
            "confirmation_status": ["同花顺已核对", "正式", "正式"],
        }
    )

    series = futures_account_series(daily)
    combined = combine_account_series([series])

    assert series.daily["date"].max() == pd.Timestamp("2026-08-18")
    assert series.total_assets == 300_900.0
    assert series.warnings
    assert combined.iloc[0]["confirmation_status"] == "同花顺已核对"
    assert combined.iloc[1]["confirmation_status"] == "正式"


def test_futures_deducts_broker_only_fees_on_next_valued_day():
    daily = pd.DataFrame(
        {
            "date": ["2026-04-01", "2026-04-02", "2026-04-24", "2026-04-27"],
            "status": ["完整"] * 4,
            "daily_pnl": [100.0, 0.0, 50.0, 10.0],
            "return_base": [10_000.0, 10_100.0, 10_100.0, 10_150.0],
            "economic_equity": [10_100.0, 10_100.0, 10_150.0, 10_160.0],
            "net_pnl": [100.0, 100.0, 150.0, 160.0],
            "confirmation_status": ["正式"] * 4,
        }
    )
    # 4/25 is a weekend fee date: it belongs to the next valued day, 4/27.
    fees = pd.DataFrame({"date": ["2026-04-01", "2026-04-25"], "amount": [2.0, 6.0]})

    series = futures_account_series(daily, fees)

    assert series.daily["pnl_amount"].tolist() == [98.0, 0.0, 50.0, 4.0]
    assert series.daily["return_base"].tolist() == [10_000.0, 10_098.0, 10_098.0, 10_148.0]
    assert series.total_assets == 10_152.0
    assert series.cumulative_pnl == 152.0


def test_inception_pnl_adds_the_stored_pre_ledger_result(tmp_path, monkeypatch):
    monkeypatch.setattr("core.db.DB_PATH", tmp_path / "cache.db")
    save_pre_ledger_pnl("etf", through_date="2026-08-04", amount=1_000.00, notes="开户至建账日")
    save_pre_ledger_pnl("etf", through_date="2026-08-04", amount=1_200.00)

    record = load_pre_ledger_pnl()["etf"]
    account = _account("etf", [("2026-08-04", 0.0, 1000.0)], cumulative_pnl=-300.00)
    account.pre_ledger_pnl = record["amount"]

    assert record["through_date"] == "2026-08-04"
    assert abs(account.inception_pnl - 900.00) < 1e-9


def test_home_page_renders_overview_without_network():
    etf = _account(
        "etf",
        [("2026-09-29", 0.0, 1000.0), ("2026-09-30", 10.0, 1000.0)],
        total_assets=1010.0,
        cumulative_pnl=10.0,
        valuation_date=pd.Timestamp("2026-09-30"),
    )
    overview = {
        "accounts": [etf],
        "combined": combine_account_series([etf], start=pd.Timestamp("2026-09-29")),
        "start": pd.Timestamp("2026-09-29"),
        "inception_pnl": 10.0,
        "errors": {"futures": "缺少月结单"},
    }
    with patch("components.home.overview.load_account_overview", return_value=overview):
        app = AppTest.from_file(str(Path(__file__).parents[1] / "app.py"), default_timeout=30).run()

    assert list(app.exception) == []
    assert [item.value for item in app.subheader] == ["总净值与每日盈亏", "总收益日历"]
    assert any("期货实盘读取失败" in item.value for item in app.warning)
    assert any("合计总资产" in item.value for item in app.markdown)


def test_first_day_pnl_counts_for_an_account_starting_on_the_start_date():
    etf = _account("etf", [("2025-11-21", 30.0, 500_000.0), ("2025-11-24", 10.0, 500_030.0)])
    futures = _account("futures", [("2025-12-24", -2.0, 500.0)])

    combined = combine_account_series([etf, futures], start=combined_start_date([etf, futures]))

    assert combined.iloc[0]["pnl_amount"] == 30.0
    assert combined["cumulative_pnl"].iloc[-1] == 40.0  # 期货 12/24 晚于各账户共同估值截止日
