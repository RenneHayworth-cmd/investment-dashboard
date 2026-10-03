import pytest


@pytest.fixture(autouse=True)
def _automatic_us_quotes_disabled(monkeypatch):
    """Unrelated AppTest runs stay offline; focused US tests enable their set."""
    from services import index_us_refresh

    monkeypatch.setattr(index_us_refresh, "US_AUTO_REFRESH_INDEX_NAMES", frozenset())


@pytest.fixture(autouse=True)
def _wind_backup_disabled(monkeypatch):
    """Keep the Wind backup offline: login shells export WIND_API_KEY.

    Tests that exercise Wind set the key themselves and patch its transport.
    """
    monkeypatch.delenv("WIND_API_KEY", raising=False)
    from services.wind_source import reset_wind_cooldown

    reset_wind_cooldown()
    yield
    reset_wind_cooldown()
