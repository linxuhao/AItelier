"""Unit tests for compute_cache_stats_batch error handling.

Tests that a single run's failure in compute_cache_stats_per_step does not
crash the entire batch — it logs a warning and continues with remaining runs.
"""
from typing import Any, Dict
from unittest.mock import patch

import pytest

from api._cache_stats import compute_cache_stats_batch


def _mock_stats(hit: int, miss: int) -> Dict[str, Any]:
    """Helper: build a per-step stats dict matching _build_stats_dict output."""
    total = hit + miss
    hit_ratio = round(hit / total, 4) if total > 0 else None
    return {
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "hit_ratio": hit_ratio,
        "total_tokens": total,
    }


class TestComputeCacheStatsBatch:
    """Test suite for compute_cache_stats_batch error handling."""

    def test_empty_input(self):
        """Empty run_ids list returns empty dict."""
        assert compute_cache_stats_batch([]) == {}

    def test_all_succeed(self):
        """All runs succeed — returns expected stats."""
        run_ids = ["run-1", "run-2"]

        def mock_per_step(run_id: str):
            if run_id == "run-1":
                return {"step_0": _mock_stats(10, 5)}
            elif run_id == "run-2":
                return {"step_0": _mock_stats(20, 10)}
            return {}

        with patch(
            "api._cache_stats.compute_cache_stats_per_step",
            side_effect=mock_per_step,
        ):
            result = compute_cache_stats_batch(run_ids)

        assert "run-1" in result
        assert result["run-1"]["cache_hit_tokens"] == 10
        assert result["run-1"]["cache_miss_tokens"] == 5

        assert "run-2" in result
        assert result["run-2"]["cache_hit_tokens"] == 20
        assert result["run-2"]["cache_miss_tokens"] == 10

    def test_skips_failing_run(self, caplog):
        """A single failing run is skipped; remaining runs still return stats."""
        run_ids = ["run-ok-1", "run-bad", "run-ok-2"]

        call_count = 0

        def mock_per_step(run_id: str):
            nonlocal call_count
            call_count += 1
            if run_id == "run-bad":
                raise RuntimeError("Corrupt trace DB")
            return {"step_0": _mock_stats(5, 3)}

        with patch(
            "api._cache_stats.compute_cache_stats_per_step",
            side_effect=mock_per_step,
        ):
            result = compute_cache_stats_batch(run_ids)

        # Both good runs should be in result
        assert "run-ok-1" in result
        assert "run-ok-2" in result
        # The bad run should be absent
        assert "run-bad" not in result

        # A warning should have been logged
        warning_messages = [
            r.message for r in caplog.records if r.levelname == "WARNING"
        ]
        assert any("run-bad" in msg for msg in warning_messages)
        assert any("Corrupt trace DB" in msg for msg in warning_messages)

    def test_all_fail(self, caplog):
        """All runs fail — returns empty dict with no crash."""
        run_ids = ["run-bad-1", "run-bad-2"]

        def mock_per_step(run_id: str):
            raise RuntimeError("Failing: " + run_id)

        with patch(
            "api._cache_stats.compute_cache_stats_per_step",
            side_effect=mock_per_step,
        ):
            result = compute_cache_stats_batch(run_ids)

        assert result == {}

        warning_messages = [
            r.message for r in caplog.records if r.levelname == "WARNING"
        ]
        assert len(warning_messages) == 2

    def test_partial_data_some_runs_empty(self):
        """Runs with no token_usage data (empty per_step) are absent from result."""
        run_ids = ["run-empty", "run-with-data"]

        def mock_per_step(run_id: str):
            if run_id == "run-empty":
                return {}
            return {"step_0": _mock_stats(10, 5)}

        with patch(
            "api._cache_stats.compute_cache_stats_per_step",
            side_effect=mock_per_step,
        ):
            result = compute_cache_stats_batch(run_ids)

        # Empty run should be absent
        assert "run-empty" not in result
        # Run with data should be present
        assert "run-with-data" in result
        assert result["run-with-data"]["cache_hit_tokens"] == 10

    def test_exception_types_caught(self, caplog):
        """Various exception types (ValueError, OSError, etc.) are all caught."""
        run_ids = ["run-value-error", "run-ok"]

        def mock_per_step(run_id: str):
            if run_id == "run-value-error":
                raise ValueError("Bad parameter")
            return {"step_0": _mock_stats(3, 2)}

        with patch(
            "api._cache_stats.compute_cache_stats_per_step",
            side_effect=mock_per_step,
        ):
            result = compute_cache_stats_batch(run_ids)

        assert "run-value-error" not in result
        assert "run-ok" in result

        warning_messages = [
            r.message for r in caplog.records if r.levelname == "WARNING"
        ]
        assert any("run-value-error" in msg for msg in warning_messages)
