"""
Unit tests for QuestionMetrics and SessionMetricsTracker.

Fast, no API calls, no server startup.
"""

import json
import os
import sys
import tempfile
import logging

import pytest

backend_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'backend')
sys.path.insert(0, backend_dir)

from app.metrics import QuestionMetrics, SessionMetricsTracker


# ---------------------------------------------------------------------------
# Tests — QuestionMetrics defaults
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestQuestionMetricsDefaults:
    """New QuestionMetrics should have sensible zero/empty defaults."""

    def test_question_metrics_defaults(self):
        m = QuestionMetrics()
        assert m.session_id == ""
        assert m.question_id == ""
        assert m.question_text == ""
        assert m.question_type == ""
        assert m.ttft_ms == 0.0
        assert m.ttfc_ms == 0.0
        assert m.total_generation_ms == 0.0
        assert m.filler_delivery_ms == 0.0
        assert m.total_cards == 0
        assert m.errors == []
        assert m.prediction_attempted is False
        assert m.prediction_correct is False
        assert m.operator_override is False

    def test_errors_list_is_independent(self):
        """Each instance should have its own errors list (not shared)."""
        m1 = QuestionMetrics()
        m2 = QuestionMetrics()
        m1.errors.append("oops")
        assert m2.errors == []


# ---------------------------------------------------------------------------
# Tests — Timing computation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTimingComputation:
    """Setting _t_* timestamps and calling log() should compute ms deltas correctly."""

    def test_timing_computation(self):
        m = QuestionMetrics()
        # Simulate timestamps (in seconds, as returned by time.monotonic())
        m._t_end_of_turn = 100.0
        m._t_filler_sent = 100.150
        m._t_first_token = 100.200
        m._t_first_card = 100.250
        m._t_generation_done = 101.500
        m._t_classify_done = 100.050

        # log() computes the deltas — we call it to trigger the computation.
        # It writes to the metrics logger, which is fine for testing.
        m.log()

        assert m.filler_delivery_ms == 150.0
        assert m.ttft_ms == 200.0
        assert m.ttfc_ms == 250.0
        assert m.total_generation_ms == 1500.0
        assert m.classify_ms == 50.0

    def test_timing_no_end_of_turn(self):
        """If _t_end_of_turn is 0, no deltas should be computed."""
        m = QuestionMetrics()
        m._t_filler_sent = 100.150
        m._t_first_token = 100.200
        m.log()
        # Deltas stay at default 0 because _t_end_of_turn was falsy
        assert m.filler_delivery_ms == 0.0
        assert m.ttft_ms == 0.0

    def test_timing_partial_timestamps(self):
        """Only deltas with both endpoints should be computed."""
        m = QuestionMetrics()
        m._t_end_of_turn = 50.0
        m._t_first_token = 50.300
        # _t_filler_sent not set — filler_delivery_ms stays 0
        m.log()
        assert m.ttft_ms == 300.0
        assert m.filler_delivery_ms == 0.0


# ---------------------------------------------------------------------------
# Tests — SessionMetricsTracker aggregates
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSessionTrackerAggregates:
    """Multiple questions should compute correct averages."""

    def test_session_tracker_aggregates(self):
        tracker = SessionMetricsTracker(session_id="test-session-001")

        # Question 1
        m1 = tracker.start_question("Tell me about yourself")
        m1._t_end_of_turn = 10.0
        m1._t_filler_sent = 10.100  # 100ms
        m1._t_first_card = 10.200   # 200ms
        m1._t_generation_done = 11.0  # 1000ms
        m1.prediction_attempted = True
        m1.prediction_prefetch_hit = True
        tracker.finish_question()

        # Question 2
        m2 = tracker.start_question("How would you design a cache")
        m2._t_end_of_turn = 20.0
        m2._t_filler_sent = 20.200  # 200ms
        m2._t_first_card = 20.300   # 300ms
        m2._t_generation_done = 22.0  # 2000ms
        m2.prediction_attempted = True
        m2.prediction_prefetch_hit = False
        m2.operator_override = True
        tracker.finish_question()

        assert tracker.question_count == 2
        assert tracker.prediction_attempts == 2
        assert tracker.prediction_hits == 1
        assert tracker.operator_overrides == 1

        summary = tracker.get_summary()
        assert summary["question_count"] == 2
        # avg filler: (100 + 200) / 2 = 150
        assert summary["avg_filler_ms"] == 150.0
        # avg ttfc: (200 + 300) / 2 = 250
        assert summary["avg_ttfc_ms"] == 250.0
        # avg gen: (1000 + 2000) / 2 = 1500
        assert summary["avg_generation_ms"] == 1500.0
        # prediction accuracy: 1/2 = 50%
        assert summary["prediction_accuracy"] == 50.0

    def test_empty_session_summary(self):
        """A session with no questions should return zero averages without crashing."""
        tracker = SessionMetricsTracker(session_id="empty")
        summary = tracker.get_summary()
        assert summary["question_count"] == 0
        assert summary["avg_filler_ms"] == 0.0
        assert summary["avg_ttfc_ms"] == 0.0

    def test_error_tracking(self):
        tracker = SessionMetricsTracker(session_id="err-test")
        m = tracker.start_question("broken question")
        m.record_error("timeout from Claude")
        m.record_error("retry failed")
        tracker.finish_question()
        assert tracker.error_count == 2


# ---------------------------------------------------------------------------
# Tests — JSONL log output
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLogWritesJsonl:
    """Calling .log() should write a valid JSON line to the metrics file."""

    def test_log_writes_jsonl(self, tmp_path):
        # Redirect the metrics logger to a temp file so we don't pollute /tmp
        jsonl_file = tmp_path / "test-metrics.jsonl"

        # Swap handler on the metrics_logger
        from app.metrics import metrics_logger
        original_handlers = metrics_logger.handlers[:]
        metrics_logger.handlers.clear()

        tmp_handler = logging.FileHandler(str(jsonl_file), mode="a")
        tmp_handler.setFormatter(logging.Formatter("%(message)s"))
        metrics_logger.addHandler(tmp_handler)

        try:
            m = QuestionMetrics(
                session_id="log-test",
                question_id="q001",
                question_type="behavioral",
                ttft_ms=123.4,
            )
            m.log()
            tmp_handler.flush()

            lines = jsonl_file.read_text().strip().splitlines()
            assert len(lines) >= 1

            data = json.loads(lines[-1])
            assert data["session_id"] == "log-test"
            assert data["question_id"] == "q001"
            assert data["question_type"] == "behavioral"
            # Internal _t_* fields should be excluded
            assert not any(k.startswith("_t_") for k in data)
        finally:
            # Restore original handlers
            metrics_logger.handlers.clear()
            for h in original_handlers:
                metrics_logger.addHandler(h)

    def test_log_answer_text_truncated(self):
        """Answer text longer than 200 chars should be truncated in the log."""
        m = QuestionMetrics()
        m.answer_text = "x" * 500
        m.log()
        assert len(m.answer_text) == 200
