from __future__ import annotations

from services.index_source_router import fetch_index_from_source
from services.index_sources_akshare import *
from services.index_sources_akshare import __all__ as _akshare_all
from services.index_sources_eastmoney import *
from services.index_sources_eastmoney import __all__ as _eastmoney_all
from services.index_sources_tickflow import *
from services.index_sources_tickflow import __all__ as _tickflow_all
from services.index_sources_yahoo import *
from services.index_sources_yahoo import __all__ as _yahoo_all

__all__ = [*_yahoo_all, *_eastmoney_all, *_akshare_all, "fetch_index_from_source", *_tickflow_all]
