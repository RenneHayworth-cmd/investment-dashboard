"""Export synthetic (never personal) service output for browser smoke tests."""
import json
from pathlib import Path
import sys
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_position_web import items, payload, NOW
from services import position_timing

with patch.object(position_timing, '_load_position_index_timing_history', return_value=None):
    formal = items()
    quotes = {item.code: {'symbol':item.code, 'price':130., 'quote_time':NOW} for item in formal}
    data = payload(formal, quotes)
Path('/tmp/position-web-fixture.json').write_text(json.dumps(data, ensure_ascii=False))
