"""
Metrics Tracker — Comprehensive logging for pipeline performance.

Tracks every stage of the interview coaching pipeline:
- Audio → transcription latency
- Question classification time
- Filler generation and delivery time
- TTFT (Time to First Token/Card visible)
- Full answer generation time
- Card count, relay count, auto vs manual
- Operator overrides
- Prediction accuracy
- Error rates

Goal: TTFT < 200ms (filler card visible within 200ms of EndOfTurn)
"""

import time
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("nohuman.metrics")

# Separate logger for structured metrics (JSON lines)
metrics_logger = logging.getLogger("nohuman.metrics.data")
_handler = logging.FileHandler("/tmp/smadprox-metrics.jsonl", mode="a")
_handler.setFormatter(logging.Formatter("%(message)s"))
metrics_logger.addHandler(_handler)
metrics_logger.setLevel(logging.INFO)
metrics_logger.propagate = False


@dataclass
class QuestionMetrics:
    """Metrics for a single interviewer question → coaching answer cycle."""

    session_id: str = ""
    question_id: str = ""  # auto-generated
    timestamp: float = 0.0

    # Question
    question_text: str = ""
    question_word_count: int = 0
    question_type: str = ""  # behavioral, system_design, coding, etc.

    # Classification
    classify_method: str = ""  # "groq" or "local"
    classify_ms: float = 0.0
    classify_confidence: float = 0.0

    # Prediction (from partial transcript)
    prediction_attempted: bool = False
    prediction_type: str = ""
    prediction_confidence: float = 0.0
    prediction_correct: bool = False  # validated on EndOfTurn
    prediction_prefetch_hit: bool = False  # did we use pre-fetched answer?

    # Filler
    filler_generated: bool = False
    filler_text: str = ""
    filler_delivery_ms: float = 0.0  # from EndOfTurn → filler card_push sent
    filler_source: str = ""  # "phase1_generic", "phase2_keyword", "phase3_specific", "predictive"

    # Answer generation
    provider: str = ""  # "claude", "groq_quick", "prefetch"
    ttft_ms: float = 0.0  # Time to First Token from Claude
    ttfc_ms: float = 0.0  # Time to First Card visible (push sent)
    total_generation_ms: float = 0.0
    answer_word_count: int = 0
    answer_text: str = ""  # first 200 chars

    # Cards
    total_cards: int = 0
    cards_auto_relayed: int = 0
    cards_manually_relayed: int = 0
    card_updates_sent: int = 0  # how many card_update messages (streaming)

    # Operator
    operator_override: bool = False
    operator_spoke: bool = False
    operator_card_count: int = 0

    # Errors
    errors: list[str] = field(default_factory=list)

    # Timestamps (internal, for computing deltas)
    _t_end_of_turn: float = 0.0
    _t_filler_sent: float = 0.0
    _t_classify_done: float = 0.0
    _t_first_token: float = 0.0
    _t_first_card: float = 0.0
    _t_generation_done: float = 0.0

    def log(self):
        """Write this metric to the structured log."""
        # Compute final deltas
        if self._t_end_of_turn:
            if self._t_filler_sent:
                self.filler_delivery_ms = round((self._t_filler_sent - self._t_end_of_turn) * 1000, 1)
            if self._t_first_token:
                self.ttft_ms = round((self._t_first_token - self._t_end_of_turn) * 1000, 1)
            if self._t_first_card:
                self.ttfc_ms = round((self._t_first_card - self._t_end_of_turn) * 1000, 1)
            if self._t_generation_done:
                self.total_generation_ms = round((self._t_generation_done - self._t_end_of_turn) * 1000, 1)
            if self._t_classify_done:
                self.classify_ms = round((self._t_classify_done - self._t_end_of_turn) * 1000, 1)

        # Truncate answer text for logging
        self.answer_text = self.answer_text[:200] if self.answer_text else ""

        # Log summary to console
        status = "OK" if not self.errors else f"ERRORS({len(self.errors)})"
        logger.info(
            f"[Metrics] {status} | session={self.session_id} | "
            f"type={self.question_type} | provider={self.provider} | "
            f"filler={self.filler_delivery_ms}ms | "
            f"ttft={self.ttft_ms}ms | ttfc={self.ttfc_ms}ms | "
            f"total={self.total_generation_ms}ms | "
            f"cards={self.total_cards} (auto={self.cards_auto_relayed}) | "
            f"words={self.answer_word_count} | "
            f"predict={'HIT' if self.prediction_prefetch_hit else 'miss' if self.prediction_attempted else 'n/a'} | "
            f"operator={'YES' if self.operator_override else 'no'}"
        )

        # Log full structured data to JSONL file
        data = asdict(self)
        # Remove internal timestamps
        data = {k: v for k, v in data.items() if not k.startswith('_t_')}
        metrics_logger.info(json.dumps(data))

    def record_error(self, msg: str):
        self.errors.append(msg)
        logger.error(f"[Metrics] Error in {self.session_id}: {msg}")


class SessionMetricsTracker:
    """Tracks aggregate metrics for an entire session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.start_time = time.monotonic()
        self.question_count = 0
        self.total_filler_ms = 0.0
        self.total_ttfc_ms = 0.0
        self.total_generation_ms = 0.0
        self.prediction_attempts = 0
        self.prediction_hits = 0
        self.operator_overrides = 0
        self.error_count = 0
        self.current: Optional[QuestionMetrics] = None

    def start_question(self, question_text: str) -> QuestionMetrics:
        """Start tracking a new question."""
        self.question_count += 1
        m = QuestionMetrics(
            session_id=self.session_id,
            question_id=f"q{self.question_count:03d}",
            timestamp=time.time(),
            question_text=question_text[:500],
            question_word_count=len(question_text.split()),
            _t_end_of_turn=time.monotonic(),
        )
        self.current = m
        return m

    def finish_question(self):
        """Finalize and log the current question metrics."""
        if not self.current:
            return
        m = self.current
        m.log()

        # Update aggregates
        self.total_filler_ms += m.filler_delivery_ms
        self.total_ttfc_ms += m.ttfc_ms
        self.total_generation_ms += m.total_generation_ms
        if m.prediction_attempted:
            self.prediction_attempts += 1
            if m.prediction_prefetch_hit:
                self.prediction_hits += 1
        if m.operator_override:
            self.operator_overrides += 1
        self.error_count += len(m.errors)

        self.current = None

    def get_summary(self) -> dict:
        """Get session-level summary metrics."""
        elapsed = time.monotonic() - self.start_time
        avg_filler = self.total_filler_ms / max(self.question_count, 1)
        avg_ttfc = self.total_ttfc_ms / max(self.question_count, 1)
        avg_gen = self.total_generation_ms / max(self.question_count, 1)
        pred_rate = self.prediction_hits / max(self.prediction_attempts, 1)

        summary = {
            "session_id": self.session_id,
            "duration_minutes": round(elapsed / 60, 1),
            "question_count": self.question_count,
            "avg_filler_ms": round(avg_filler, 1),
            "avg_ttfc_ms": round(avg_ttfc, 1),
            "avg_generation_ms": round(avg_gen, 1),
            "prediction_accuracy": round(pred_rate * 100, 1),
            "operator_overrides": self.operator_overrides,
            "error_count": self.error_count,
            "ttfc_under_200ms": sum(1 for _ in range(1)),  # placeholder
        }

        logger.info(f"[Metrics] Session summary: {json.dumps(summary)}")
        return summary
