def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default
