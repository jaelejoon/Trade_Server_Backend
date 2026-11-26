TRADING_SYMBOL = "BTCUSDT"

SYMBOL_LIMIT_MESSAGE = f"거래 종목은 {TRADING_SYMBOL}만 허용됩니다."


def normalize_trading_symbol(value: str) -> str:
    if not isinstance(value, str) or value.strip().upper() != TRADING_SYMBOL:
        raise ValueError(SYMBOL_LIMIT_MESSAGE)
    return TRADING_SYMBOL
