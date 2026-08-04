"""Generic parquet persistence for feature-engine datasets.

Any column holding Python dict/list objects (e.g. `displacement_reference`,
`reasons`, `metadata`) is JSON-encoded before writing, since Arrow/Parquet
cannot serialize arbitrary nested Python objects directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def save_feature_dataset(df: pd.DataFrame, path: str | Path, index: bool = False) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    for col in out.columns:
        if out[col].apply(lambda v: isinstance(v, (dict, list, tuple))).any():
            out[col] = out[col].apply(lambda v: json.dumps(v, default=str))
    out.to_parquet(p, index=index)
