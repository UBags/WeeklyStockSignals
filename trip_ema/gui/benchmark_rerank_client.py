"""GUI-side passthrough for benchmark rerank queries.

The actual fetch_rank_scores lives in notifications/benchmark_rerank.py
since that's where email composition consumes it. The GUI shouldn't
reach across into the notifications package for layering hygiene, so
this module re-exports the function with the same signature.

Phase 6-3.
"""
from __future__ import annotations

from ..notifications.benchmark_rerank import fetch_rank_scores

__all__ = ["fetch_rank_scores"]
