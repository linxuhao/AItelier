"""Unit tests for compute_cache_stats_batch error handling.

Tests that a single run's failure in compute_cache_stats_per_step does not
crash the entire batch — it logs a warning and continues with remaining runs.
"""
import json
import sqlite3
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import patch

import pytest

from api._cache_stats import compute_cache_stats_batch


def _mock_stats(hit: int, miss: int, prompt: int = 0, completion: int = 0) -> Dict[str, Any]:
    """Helper: build a per-step stats dict matching _build_stats_dict output."""
    from api._cache_stats import _build_stats_dict
    return _build_stats_dict(hit, miss, prompt, completion)


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


class TestUnknownCacheRowsStayOutOfTheRatio:
    """A turn whose provider reported no cache info must not be counted a miss.

    End-to-end over the real path: AIGateway._extract_usage builds the payload,
    it is stored as JSON exactly as dpe_pipeline traces it, and the real SQL
    (COALESCE over json_extract) aggregates it. That is the whole chain in
    which an unmeasured turn either stays out of the denominator or silently
    becomes a full miss.
    """

    @staticmethod
    def _usage(prompt, completion, **cache_fields):
        """One traced turn, as the gateway would record it."""
        from core.ai_router import AIGateway
        return AIGateway._extract_usage(SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion, **cache_fields)))

    @staticmethod
    def _fake_skillflow(rows):
        """rows: list of (step_id, usage payload) for a single run 'run-x'."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE skillflow_trace (run_id TEXT, step_id TEXT, "
            "category TEXT, event TEXT, payload_json TEXT)"
        )
        for step_id, payload in rows:
            conn.execute(
                "INSERT INTO skillflow_trace VALUES (?, ?, 'usage', "
                "'token_usage', ?)",
                ("run-x", step_id, json.dumps(payload)),
            )
        conn.commit()

        class _SF:
            def trace_query(self, run_id, sql, params):
                return conn.execute(sql, params).fetchall()

        return _SF()

    def _stats(self, rows):
        sf = self._fake_skillflow(rows)
        with patch("api.dependencies.get_skillflow", return_value=sf):
            return compute_cache_stats_batch(["run-x"])["run-x"]

    def test_mixed_known_and_unknown_uses_known_rows_only(self):
        """A silent-provider turn adds nothing to either side of the ratio."""
        stats = self._stats([
            ("1", self._usage(1000, 50, prompt_cache_hit_tokens=800,
                              prompt_cache_miss_tokens=200)),
            ("2", self._usage(9000, 60)),   # Ollama Cloud shape: says nothing
        ])
        assert stats["cache_hit_tokens"] == 800
        assert stats["cache_miss_tokens"] == 200
        # Counting the unmeasured 9000 as a miss would give 800/10000 = 0.08.
        assert stats["hit_ratio"] == 0.8
        # …but keeping it out of the RATIO must not keep it out of the TOTAL.
        # This line used to read `total_tokens == 1000`, pinning the subset as
        # the total; measured on jinyong-numbers 2026-09-02 that showed 3.4M
        # for a run that had processed 78.8M.
        assert stats["covered_tokens"] == 1000
        assert stats["prompt_tokens"] == 10000
        assert stats["completion_tokens"] == 110
        assert stats["total_tokens"] == 10110

    def test_all_unknown_reports_undefined_not_zero(self):
        """No measured tokens at all -> undefined ratio, never a 0% claim."""
        stats = self._stats([
            ("1", self._usage(4000, 30)),
            ("2", self._usage(6000, 40)),
        ])
        assert stats["hit_ratio"] is None
        assert stats["covered_tokens"] == 0
        # Unknown cache accounting is not zero work: the tokens were processed.
        assert stats["total_tokens"] == 10070

    def test_all_measured_zero_hit_reports_a_real_zero(self):
        """A provider that DID report cached_tokens=0 aggregates to a real 0.0,
        which is what "undefined" above must stay distinguishable from."""
        stats = self._stats([
            ("1", self._usage(4000, 30,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=0))),
        ])
        assert stats["hit_ratio"] == 0.0
        assert stats["covered_tokens"] == 4000
        assert stats["total_tokens"] == 4030


class TestMergeStatsIsTheOnlyMerge:
    """run_routers and repo_routers each re-derived total/ratio inline; now
    there is one merge and it must carry every field."""

    def test_merge_from_none_copies(self):
        from api._cache_stats import merge_stats, _build_stats_dict
        s = _build_stats_dict(8, 2, 100, 10)
        assert merge_stats(None, s) == s

    def test_merge_sums_all_four_and_rederives(self):
        from api._cache_stats import merge_stats, _build_stats_dict
        a = _build_stats_dict(8, 2, 100, 10)
        b = _build_stats_dict(0, 0, 9000, 60)   # a silent-provider step
        m = merge_stats(a, b)
        assert m["cache_hit_tokens"] == 8 and m["cache_miss_tokens"] == 2
        assert m["covered_tokens"] == 10
        assert m["hit_ratio"] == 0.8            # silent step stays out of the ratio
        assert m["prompt_tokens"] == 9100
        assert m["completion_tokens"] == 70
        assert m["total_tokens"] == 9170        # …and inside the total

    def test_the_routers_use_it(self):
        import inspect, api.run_routers, api.repo_routers
        for mod in (api.run_routers, api.repo_routers):
            src = inspect.getsource(mod)
            assert "merge_stats(" in src, mod.__name__
            assert 'merged["cache_hit_tokens"] +=' not in src, mod.__name__
