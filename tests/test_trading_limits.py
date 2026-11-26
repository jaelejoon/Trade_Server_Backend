import pytest
from pydantic import ValidationError

from app.schemas import TradingAccountCreateRequest


def test_trading_requests_reject_other_symbols():
    with pytest.raises(ValidationError):
        TradingAccountCreateRequest(api_key_id=1, symbol="ETHUSDT")
