"""Process-wide safety net: no outbound HTTP call may wait forever.

Some third-party fetchers (e.g. AkShare's EastMoney board history) call
``requests.get`` without a timeout. From the server, EastMoney sometimes stops
answering on an open connection; with no read timeout that call blocks for
good, and because the coordinator refreshes serially, every later ETF, quote
and formal-close update stalls behind it (seen 2026-09-28 23:33 to 09-29).
Calls that pass their own timeout are left untouched.
"""
from core.network import DEFAULT_TIMEOUT, install_default_request_timeout
