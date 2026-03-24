"""
Predictive Engine — Iterative question classification as Deepgram tokens arrive.

Called from pipeline.py on every Deepgram Update/StartOfTurn event.
Progressively classifies the question, pre-fetches matching script sections,
and generates increasingly relevant fillers.

Latency is the priority. Multiple Groq calls during a single question are fine.
"""

import re
import asyncio
from dataclasses import dataclass, field
from typing import Optional

# Question types
QUESTION_TYPES = [
    "behavioral",
    "system_design",
    "coding",
    "project_deep_dive",
    "intro_about_you",
    "why_company",
    "follow_up",
    "clarification",
    "questions_for_interviewer",
    "unknown",
]

# Keyword patterns for fast local classification (no API call needed)
KEYWORD_PATTERNS = {
    "behavioral": [
        r"tell me about a time",
        r"give me an example",
        r"describe a situation",
        r"how did you handle",
        r"what would you do if",
        r"conflict",
        r"challenge",
        r"difficult",
        r"proud of",
        r"failure",
        r"mistake",
        r"leadership",
        r"disagree",
    ],
    "system_design": [
        r"how would you design",
        r"design a system",
        r"architect",
        r"scale",
        r"high level design",
        r"whiteboard",
        r"distributed",
        r"microservices",
    ],
    "coding": [
        r"write a function",
        r"implement",
        r"algorithm",
        r"data structure",
        r"time complexity",
        r"space complexity",
        r"optimize",
        r"leetcode",
        r"coding",
    ],
    "intro_about_you": [
        r"tell me about yourself",
        r"walk me through your",
        r"background",
        r"introduce yourself",
        r"career",
    ],
    "why_company": [
        r"why .+company",
        r"why .+role",
        r"why are you interested",
        r"what attracted you",
        r"why do you want",
    ],
    "follow_up": [
        r"can you elaborate",
        r"tell me more",
        r"go deeper",
        r"what about",
        r"how about",
    ],
    "questions_for_interviewer": [
        r"do you have .+ questions",
        r"questions for me",
        r"anything you.+want to ask",
    ],
}


@dataclass
class ScriptSection:
    """A parsed section from script.md that can be matched against questions."""
    heading: str
    content: str
    keywords: list[str] = field(default_factory=list)
    question_type: str = "unknown"


@dataclass
class PredictionResult:
    """Result of a prediction attempt."""
    question_type: str
    confidence: float  # 0.0 - 1.0
    matched_section: Optional[ScriptSection] = None
    filler_text: Optional[str] = None
    prefetched_answer: Optional[str] = None


class PredictiveEngine:
    """
    Iteratively classifies interviewer questions as tokens arrive.
    Pre-fetches matching script sections for <500ms card delivery.
    """

    def __init__(self, script_content: str = ""):
        self.script_sections: list[ScriptSection] = []
        self.current_text = ""
        self.token_count = 0
        self.current_prediction: Optional[PredictionResult] = None
        self.prefetched_cards = None
        self._groq_client = None

        if script_content:
            self.parse_script(script_content)

    def parse_script(self, content: str):
        """Parse script.md into searchable sections."""
        self.script_sections = []
        # Split on ## headings
        sections = re.split(r'^(##\s+.+)$', content, flags=re.MULTILINE)

        current_heading = ""
        for part in sections:
            if part.startswith('## '):
                current_heading = part.strip('# ').strip()
            elif current_heading and part.strip():
                section = ScriptSection(
                    heading=current_heading,
                    content=part.strip(),
                    keywords=self._extract_keywords(current_heading + " " + part[:500]),
                    question_type=self._classify_section_type(current_heading),
                )
                self.script_sections.append(section)

    def _extract_keywords(self, text: str) -> list[str]:
        """Extract meaningful keywords from text for matching."""
        # Remove common stop words, keep technical terms
        stop_words = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "have", "has", "had", "do", "does", "did", "will", "would",
            "could", "should", "may", "might", "can", "shall", "to", "of",
            "in", "for", "on", "with", "at", "by", "from", "as", "into",
            "through", "during", "before", "after", "above", "below",
            "between", "and", "but", "or", "not", "no", "so", "if",
            "then", "than", "that", "this", "these", "those", "what",
            "which", "who", "whom", "how", "when", "where", "why",
            "all", "each", "every", "both", "few", "more", "most",
            "other", "some", "such", "only", "own", "same", "just",
            "also", "very", "about", "up", "out", "its", "it",
            "i", "my", "me", "we", "our", "you", "your", "they",
        }
        words = re.findall(r'\b[a-z][a-z0-9]+\b', text.lower())
        return list(set(w for w in words if w not in stop_words and len(w) > 2))

    def _classify_section_type(self, heading: str) -> str:
        """Classify a script section by its heading."""
        h = heading.lower()
        if any(w in h for w in ["behavioral", "star", "story", "stories"]):
            return "behavioral"
        if any(w in h for w in ["system design", "architecture", "whiteboard", "phase"]):
            return "system_design"
        if any(w in h for w in ["coding", "technical", "algorithm", "code"]):
            return "coding"
        if any(w in h for w in ["intro", "about", "background"]):
            return "intro_about_you"
        if any(w in h for w in ["why", "motivation"]):
            return "why_company"
        if any(w in h for w in ["question", "anticipated", "q&a"]):
            return "behavioral"  # Q&A sections contain mixed types
        if any(w in h for w in ["deep dive", "project", "technical"]):
            return "project_deep_dive"
        return "unknown"

    def reset(self):
        """Reset state for a new question."""
        self.current_text = ""
        self.token_count = 0
        self.current_prediction = None
        self.prefetched_cards = None

    def _local_classify(self, text: str) -> tuple[str, float]:
        """Fast local classification using keyword patterns. No API call."""
        text_lower = text.lower()
        best_type = "unknown"
        best_score = 0.0

        for qtype, patterns in KEYWORD_PATTERNS.items():
            score = 0.0
            for pattern in patterns:
                if re.search(pattern, text_lower):
                    score += 1.0 / len(patterns)
            if score > best_score:
                best_score = score
                best_type = qtype

        # Boost confidence based on text length
        if self.token_count > 15:
            best_score = min(1.0, best_score * 1.3)

        return best_type, best_score

    def _match_section(self, text: str, question_type: str) -> Optional[ScriptSection]:
        """Find the best matching script section for the given text and type."""
        text_keywords = set(self._extract_keywords(text))
        if not text_keywords:
            return None

        best_section = None
        best_overlap = 0.0

        for section in self.script_sections:
            # Prefer sections matching the question type
            type_bonus = 0.2 if section.question_type == question_type else 0.0

            section_keywords = set(section.keywords)
            if not section_keywords:
                continue

            overlap = len(text_keywords & section_keywords) / max(len(text_keywords), 1)
            score = overlap + type_bonus

            if score > best_overlap:
                best_overlap = score
                best_section = section

        if best_overlap > 0.15:  # Minimum threshold
            return best_section
        return None

    def _generate_filler(self, prediction: PredictionResult) -> str:
        """Generate a filler line based on current prediction confidence."""
        if prediction.confidence < 0.3:
            # Generic filler
            return "Yeah, so..."

        if prediction.question_type == "behavioral":
            return "Yeah, so that reminds me of a situation I dealt with..."
        elif prediction.question_type == "system_design":
            return "So thinking about the architecture here..."
        elif prediction.question_type == "coding":
            return "Alright, let me think through the approach..."
        elif prediction.question_type == "intro_about_you":
            return "Sure, so a bit about my background..."
        elif prediction.question_type == "why_company":
            return "Yeah, so what really drew me to this role..."
        elif prediction.question_type == "project_deep_dive":
            return "So let me walk you through that project..."
        elif prediction.question_type == "follow_up":
            return "Right, so to go deeper on that..."

        # If we have a matched section, use its opening
        if prediction.matched_section:
            first_sentence = prediction.matched_section.content.split('.')[0]
            if len(first_sentence) > 20 and len(first_sentence) < 150:
                return first_sentence.strip() + "..."

        return "Yeah, so..."

    async def on_partial_transcript(self, text: str) -> Optional[PredictionResult]:
        """
        Called on every Deepgram Update/StartOfTurn event.
        Returns a PredictionResult if we have enough confidence, else None.
        """
        self.current_text = text
        words = text.strip().split()
        self.token_count = len(words)

        # Too early — accumulate
        if self.token_count < 5:
            return None

        # Local classification (instant, no API call)
        question_type, confidence = self._local_classify(text)

        # Try to match a script section
        matched = self._match_section(text, question_type)

        # Build prediction
        if matched:
            confidence = min(1.0, confidence + 0.2)  # Boost for section match

        prediction = PredictionResult(
            question_type=question_type,
            confidence=confidence,
            matched_section=matched,
        )

        # Generate filler
        prediction.filler_text = self._generate_filler(prediction)

        # If high confidence and we have a matched section, pre-fetch the answer
        if confidence > 0.7 and matched:
            prediction.prefetched_answer = matched.content

        self.current_prediction = prediction

        # Only return if we have something useful
        if self.token_count >= 5:
            return prediction
        return None

    async def on_end_of_turn(self, full_text: str) -> Optional[str]:
        """
        Called on Deepgram EndOfTurn. Returns pre-fetched answer if prediction was correct.
        Returns None if prediction was wrong (caller should generate normally).
        """
        if not self.current_prediction or not self.current_prediction.prefetched_answer:
            self.reset()
            return None

        # Validate: re-classify the full question and check if it matches
        final_type, final_confidence = self._local_classify(full_text)

        if (final_type == self.current_prediction.question_type and
            final_confidence > 0.5 and
            self.current_prediction.confidence > 0.6):
            answer = self.current_prediction.prefetched_answer
            self.reset()
            return answer

        # Prediction was wrong — discard
        self.reset()
        return None
