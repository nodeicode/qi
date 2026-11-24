"""Backward-compatible import location for the generic Arrow cache."""

from .core import ArrowIpcCache
from .models import CacheKey, CacheRead, DatasetSpec, SimpleDatasetSpec

__all__ = ["ArrowIpcCache", "CacheKey", "CacheRead", "DatasetSpec", "SimpleDatasetSpec"]
