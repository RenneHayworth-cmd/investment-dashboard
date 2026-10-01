"""Transient holdings previews shared across browser/page sessions."""
from copy import deepcopy
from threading import Lock

_LOCK = Lock()
_PREVIEWS: dict[tuple, dict] = {}
_KEYS = (
    "position_etf_morning_timing_preview",
    "position_etf_lunch_timing_preview",
    "position_etf_realtime_timing_preview",
    "position_derivative_realtime_preview",
    "position_index_realtime_preview",
    "position_auxiliary_quote_refresh_state",
    "position_etf_auto_final_last_attempt",
)


def restore_previews(session, scope: tuple, market_now) -> None:
    """Restore only today's previews for this exact configuration; no disk writes."""
    day = market_now.date().isoformat()
    previous_scope = session.get("position_preview_scope")
    previous_day = session.get("position_preview_date")
    if (previous_scope is not None and previous_scope != scope) or (
        previous_day is not None and previous_day != day
    ):
        for key in _KEYS:
            session.pop(key, None)
    with _LOCK:
        for key in list(_PREVIEWS):
            if _PREVIEWS[key]["date"] != day:
                del _PREVIEWS[key]
        saved = deepcopy(_PREVIEWS.get(scope, {}))
    for key, value in saved.get("states", {}).items():
        if key not in session:
            session[key] = value
    session["position_preview_scope"] = scope
    session["position_preview_date"] = day


def remember_previews(session, market_now) -> None:
    scope = session.get("position_preview_scope")
    if scope is None:
        return
    states = {key: deepcopy(session[key]) for key in _KEYS if key in session}
    with _LOCK:
        _PREVIEWS[scope] = {"date": market_now.date().isoformat(), "states": states}
