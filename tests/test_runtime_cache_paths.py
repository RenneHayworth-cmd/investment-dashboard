import sqlite3

import pandas as pd
import pytest

from core import cache


@pytest.mark.parametrize("foreign", ["/runtime/data/raw/test/ETF_1d.csv",
                                    "/srv/investment-dashboard/data/raw/test/ETF_1d.csv",
                                    "C:/old/dashboard/data/raw/test/ETF_1d.csv"])
def test_moved_runtime_reads_identical_dataset_without_metadata_writes(tmp_path, monkeypatch, foreign):
    db_path = tmp_path / "cache.db"
    monkeypatch.setattr(cache, "RAW_DIR", tmp_path / "data/raw")
    monkeypatch.setattr(cache, "get_conn", lambda: sqlite3.connect(db_path))
    path, _ = cache._dataset_paths("ETF", "test", "1d")
    path.parent.mkdir(parents=True)
    expected = pd.DataFrame({"date": ["2026-09-09", "2026-09-10"], "price": [1.25, 1.26]})
    expected.to_csv(path, index=False)
    original = path.read_bytes()
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE datasets(symbol, source, data_type, period, file_path, last_trade_date, last_update_time, status)")
        db.execute("INSERT INTO datasets VALUES('ETF','test','fund_close_raw','1d',?,'2026-09-10','2026-09-10 15:10:00','success')", (foreign,))
    loaded, meta = cache.load_dataset("ETF", "test", "fund_close_raw")
    pd.testing.assert_frame_equal(loaded, expected)
    assert path.read_bytes() == original
    assert meta['last_trade_date'] == '2026-09-10'
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT file_path FROM datasets").fetchone()[0] == foreign
        db.execute("UPDATE datasets SET status='failed'")
    assert cache.load_dataset("ETF", "test", "fund_close_raw")[0] is None
    assert cache.load_dataset("OTHER", "test", "fund_close_raw")[0] is None
