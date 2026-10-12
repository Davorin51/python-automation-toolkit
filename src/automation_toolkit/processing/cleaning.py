import pandas as pd


def clean_text(frame: pd.DataFrame) -> pd.DataFrame:
    """Trim whitespace without silently inventing missing business values."""
    result = frame.copy()
    for name in ["product", "category", "order_id"]:
        if name in result:
            result[name] = result[name].astype("string").fillna("").str.strip()
    return result
