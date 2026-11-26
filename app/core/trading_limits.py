TRADING_SYMBOL = "BTCUSDT"
MAX_LEVERAGE = 2

SYMBOL_LIMIT_MESSAGE = f"거래 종목은 {TRADING_SYMBOL}만 허용됩니다."
LEVERAGE_LIMIT_MESSAGE = f"레버리지는 1배 이상 {MAX_LEVERAGE}배 이하여야 합니다."


def normalize_trading_symbol(value: str) -> str:
    if not isinstance(value, str) or value.strip().upper() != TRADING_SYMBOL:
        raise ValueError(SYMBOL_LIMIT_MESSAGE)
    return TRADING_SYMBOL


def is_allowed_leverage(value: object) -> bool:
    return type(value) in (int, float) and 1 <= value <= MAX_LEVERAGE
