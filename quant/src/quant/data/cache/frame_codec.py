"""Explicit pandas/Arrow conversion at cache domain boundaries."""

from __future__ import annotations

import pandas as pd
import pyarrow as pa


def frame_to_table(data: pd.DataFrame) -> pa.Table:
    """Encode a timestamp-indexed frame without persisting pandas index metadata."""
    if not isinstance(data.index, pd.DatetimeIndex):
        raise ValueError("cache data requires a DataFrame indexed by timestamp")
    frame = data.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    index = pd.DatetimeIndex(frame.index)
    frame["timestamp"] = (
        index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    )
    return pa.Table.from_pandas(frame.reset_index(drop=True), preserve_index=False)


def table_to_frame(table: pa.Table | None) -> pd.DataFrame | None:
    """Decode canonical Arrow data into the handlers' timestamp-indexed contract."""
    if table is None:
        return None
    frame = table.to_pandas(date_as_object=False)
    if frame.empty:
        return pd.DataFrame()
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame = frame.set_index("timestamp")
    return frame.sort_index()
