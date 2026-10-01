import pytest


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
