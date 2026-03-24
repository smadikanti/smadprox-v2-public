"""
Three-Phase Filler Engine for NoHuman.

Phase 1: Instant fillers (<1ms) - pre-computed, no API call
Phase 2: Bridge sentences (~200ms) - keyword extraction + template
Phase 3: Claude streaming (1.5-3s) - handled by coach.py

Based on analysis of two real coached conversations:
- Amazon Pipelines technical interview (120 chunks)
- Netflix behavioral interview (91 chunks)
"""

import random
import re
from typing import Optional


# ---------------------------------------------------------------------------
# Filler Banks (pre-computed, zero latency)
# ---------------------------------------------------------------------------

UNIVERSAL_ACKNOWLEDGMENTS = [
    "Yeah.",
    "Sure.",
    "Right.",
    "Okay.",
    "Yeah, for sure.",
    "Understood.",
    "Got it.",
    "Yeah, yeah.",
    "Absolutely.",
]

UNIVERSAL_TRANSITIONS = [
    "So, basically,",
    "I would say,",
    "In terms of that,",
    "Specifically,",
    "I think,",
    "So yeah,",
]

# Context-adaptive filler banks
FILLER_BANKS = {
    "technical": {
        "acknowledgment": [
            "Yeah, that's correct.",
            "That is right.",
            "Yes, exactly.",
            "That makes sense.",
        ],
        "transition": [
            "I can definitely elaborate on that.",
            "So the way we approached it was,",
            "From a technical perspective,",
            "Let me walk you through that.",
            "So the architecture uses,",
        ],
        "appreciation": [
            "That's really helpful context.",
            "That's a good question.",
            "Great question.",
        ],
        "bridge_templates": [
            "So in terms of {keyword},",
            "Yeah, {keyword} is something I've worked with.",
            "On the {keyword} side,",
            "So regarding {keyword},",
            "For {keyword} specifically,",
        ],
    },
    "behavioral": {
        "acknowledgment": [
            "That's a great question.",
            "I appreciate you asking that.",
            "Yeah, that's important.",
            "That's a fair point.",
        ],
        "transition": [
            "I would say, honestly,",
            "I think what drove me was,",
            "Looking back,",
            "So specifically on this,",
            "Let me give you some context.",
        ],
        "appreciation": [
            "That's really helpful context. Thank you.",
            "I appreciate you sharing that.",
            "That's a great point.",
        ],
        "bridge_templates": [
            "That's interesting about {keyword}.",
            "Yeah, {keyword} is something I think about a lot.",
            "In terms of {keyword},",
            "So regarding {keyword},",
            "On the {keyword} front,",
        ],
    },
    "romantic": {
        "acknowledgment": [
            "That's so cool!",
            "Oh nice!",
            "Yeah, totally.",
            "I love that.",
        ],
        "transition": [
            "I actually,",
            "So funny story,",
            "That reminds me,",
            "You know what,",
        ],
        "appreciation": [
            "That's really interesting.",
            "I love hearing about that.",
            "That's awesome.",
        ],
        "bridge_templates": [
            "I actually love {keyword} too!",
            "That's awesome about {keyword}.",
            "Oh {keyword}? I've been wanting to try that.",
            "Yeah {keyword} is something I'm into as well.",
        ],
    },
    "networking": {
        "acknowledgment": [
            "That's really interesting.",
            "Yeah, that makes a lot of sense.",
            "Absolutely.",
        ],
        "transition": [
            "In my experience,",
            "I've been thinking about that too.",
            "So from my perspective,",
        ],
        "appreciation": [
            "That's great context. Thank you.",
            "I appreciate you sharing that perspective.",
        ],
        "bridge_templates": [
            "In terms of {keyword},",
            "Yeah, {keyword} is really fascinating.",
            "On the {keyword} side of things,",
        ],
    },
    "general": {
        "acknowledgment": UNIVERSAL_ACKNOWLEDGMENTS,
        "transition": UNIVERSAL_TRANSITIONS,
        "appreciation": [
            "That's interesting.",
            "I see what you mean.",
            "That makes sense.",
        ],
        "bridge_templates": [
            "So in terms of {keyword},",
            "That's interesting about {keyword}.",
            "Yeah, regarding {keyword},",
        ],
    },
}

# Recovery chain for unexpected territory
UNEXPECTED_RECOVERY = {
    "acknowledge": [
        "Yeah, I mean, honestly,",
        "That's a fair point.",
        "I appreciate you bringing that up.",
    ],
    "reflect": [
        "I think what I'm taking away from that is,",
        "If I'm being honest,",
        "That's something I've been thinking about.",
    ],
    "forward": [
        "Going forward, I would,",
        "What I plan to do differently is,",
        "I think the key takeaway is,",
    ],
}

# Common stop words for keyword extraction
STOP_WORDS = frozenset(
    "the a an is are was were be been being have has had do does did "
    "will would shall should may might can could am i you he she it we "
    "they me him her us them my your his its our their this that these "
    "those what which who whom when where why how all each every both "
    "few more most other some such no not only own same so than too "
    "very just don also like you know kind of basically well actually "
    "really think mean okay yeah right sure gonna wanna gotta um uh "
    "and or but if then else for in on at to from by with about into "
    "through during before after above below between under again further "
    "once here there when where why how".split()
)


# ---------------------------------------------------------------------------
# Segment Classification
# ---------------------------------------------------------------------------

def classify_segment(transcript: str) -> str:
    """
    Classify the last speech segment type. Must be <10ms.
    Returns: 'specific_question', 'open_question', 'clarifying_question',
             'topic_change', 'wrap_up', 'interruption',
             'long_explanation', 'short_explanation'
    """
    text = transcript.strip().lower()
    if not text:
        return "short_explanation"

    question_starters = [
        "what", "how", "why", "when", "where", "who",
        "can you", "could you", "would you", "do you",
        "tell me", "walk me through", "describe", "explain",
        "help me understand",
    ]

    # Question detection
    is_question = text.endswith("?") or any(
        text.startswith(q) for q in question_starters
    )
    if is_question:
        if any(w in text for w in ["clarif", "mean by", "what do you mean", "sorry"]):
            return "clarifying_question"
        if len(text.split()) > 20:
            return "open_question"
        return "specific_question"

    # Topic change
    if any(w in text for w in [
        "let's talk about", "moving on", "another thing",
        "switching gears", "on a different note", "next topic",
        "let me ask you", "i wanted to",
    ]):
        return "topic_change"

    # Wrap-up
    if any(w in text for w in [
        "any questions", "anything else", "before we go",
        "that's all", "we're out of time", "last question",
        "wrapping up", "to wrap up",
    ]):
        return "wrap_up"

    # Interruption
    if "sorry" in text and any(w in text for w in ["interrupt", "go ahead", "continue"]):
        return "interruption"

    # Length-based classification
    if len(text.split()) > 40:
        return "long_explanation"
    return "short_explanation"


# ---------------------------------------------------------------------------
# Keyword Extraction
# ---------------------------------------------------------------------------

def extract_keywords(transcript: str, max_keywords: int = 3) -> list[str]:
    """
    Extract conversation-relevant keywords from the last speech segment.
    Simple heuristics, no ML - must be <100ms.
    """
    if not transcript:
        return []

    text = transcript.strip()
    words = text.split()

    # Look for multi-word technical terms and proper nouns (capitalized sequences)
    phrases = []
    current_phrase = []
    for word in words:
        clean = re.sub(r"[^\w]", "", word)
        if not clean:
            if current_phrase:
                phrases.append(" ".join(current_phrase))
                current_phrase = []
            continue
        if clean[0].isupper() and clean.lower() not in STOP_WORDS:
            current_phrase.append(clean)
        else:
            if current_phrase:
                phrases.append(" ".join(current_phrase))
                current_phrase = []
    if current_phrase:
        phrases.append(" ".join(current_phrase))

    # Filter out single-char phrases
    phrases = [p for p in phrases if len(p) > 2]

    # Also extract individual significant words
    significant_words = []
    for word in words:
        clean = re.sub(r"[^\w]", "", word).lower()
        if clean and clean not in STOP_WORDS and len(clean) > 3:
            significant_words.append(clean)

    # Combine: phrases first (more specific), then individual words
    all_keywords = phrases + significant_words

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for kw in all_keywords:
        kw_lower = kw.lower()
        if kw_lower not in seen:
            seen.add(kw_lower)
            unique.append(kw)

    return unique[:max_keywords]


# ---------------------------------------------------------------------------
# Conversation Type Detection
# ---------------------------------------------------------------------------

def detect_conversation_type(context_docs: list[dict]) -> str:
    """
    Analyze context documents to determine conversation type.
    Called once when session starts.
    """
    all_text = " ".join(
        doc.get("content", "") + " " + doc.get("title", "")
        for doc in context_docs
    ).lower()

    if any(w in all_text for w in ["interview", "job", "role", "position", "hiring"]):
        if any(w in all_text for w in [
            "system design", "coding", "technical", "architecture", "algorithm",
        ]):
            return "technical"
        if any(w in all_text for w in [
            "behavioral", "leadership", "motivation", "feedback", "star",
        ]):
            return "behavioral"
        return "technical"  # default for interviews

    if any(w in all_text for w in [
        "date", "dating", "interested in", "crush", "likes", "instagram",
        "attractive", "girlfriend", "boyfriend",
    ]):
        return "romantic"

    if any(w in all_text for w in [
        "networking", "coffee chat", "connect", "linkedin", "career",
    ]):
        return "networking"

    return "general"


# ---------------------------------------------------------------------------
# The Filler Engine
# ---------------------------------------------------------------------------

class FillerEngine:
    """
    Generates instant fillers with ZERO API latency.
    Runs entirely locally on the server.
    """

    def __init__(self, conversation_type: str = "general"):
        self.conversation_type = conversation_type
        self.bank = FILLER_BANKS.get(conversation_type, FILLER_BANKS["general"])
        self.turn_count = 0  # Track conversation progression
        self._last_fillers: list[str] = []  # Avoid repetition

    def set_conversation_type(self, conversation_type: str) -> None:
        self.conversation_type = conversation_type
        self.bank = FILLER_BANKS.get(conversation_type, FILLER_BANKS["general"])

    def _pick(self, options: list[str]) -> str:
        """Pick a random option, avoiding recent repeats."""
        available = [o for o in options if o not in self._last_fillers]
        if not available:
            available = options
            self._last_fillers.clear()
        choice = random.choice(available)
        self._last_fillers.append(choice)
        if len(self._last_fillers) > 6:
            self._last_fillers.pop(0)
        return choice

    def generate_phase1(self, segment_type: str) -> str:
        """
        Phase 1: Instant filler (<1ms). No context needed beyond segment type.
        Returns a natural acknowledgment + transition.
        """
        self.turn_count += 1

        if segment_type in ("specific_question", "open_question"):
            ack = self._pick(self.bank["acknowledgment"])
            trans = self._pick(self.bank["transition"])
            return f"{ack} {trans}"

        elif segment_type == "clarifying_question":
            return self._pick([
                "Yeah, that's correct.",
                "Yeah.",
                "Right, so,",
                "Yes, exactly.",
            ])

        elif segment_type == "long_explanation":
            return self._pick(self.bank["appreciation"])

        elif segment_type == "topic_change":
            return f"Sure. {self._pick(self.bank['transition'])}"

        elif segment_type == "wrap_up":
            return self._pick([
                "Yeah, I think that covers it.",
                "I do have a couple of questions, actually.",
                "Yeah, I think we covered a lot.",
            ])

        elif segment_type == "interruption":
            return self._pick([
                "Yeah, no problem.",
                "No worries.",
                "Sure, go ahead.",
            ])

        else:  # short_explanation or default
            return self._pick(self.bank["acknowledgment"])

    def generate_phase2(
        self,
        last_transcript: str,
        segment_type: str,
    ) -> Optional[str]:
        """
        Phase 2: Bridge sentence (~200ms). Uses keyword extraction.
        Returns a bridge that connects the filler to Claude's upcoming response.
        """
        keywords = extract_keywords(last_transcript)

        if not keywords:
            # No keywords found, use a generic transition
            return self._pick(self.bank["transition"])

        keyword = keywords[0]
        template = self._pick(self.bank["bridge_templates"])

        try:
            return template.format(keyword=keyword)
        except (KeyError, IndexError):
            return f"So in terms of {keyword},"

    def generate_unexpected_recovery(self) -> list[dict]:
        """
        Generate a multi-phase recovery chain for unexpected territory.
        Returns a list of filler phases to send sequentially.
        """
        return [
            {"phase": 1, "text": random.choice(UNEXPECTED_RECOVERY["acknowledge"])},
            {"phase": 2, "text": random.choice(UNEXPECTED_RECOVERY["reflect"])},
        ]


# ---------------------------------------------------------------------------
# Controlled Imperfection Engine
# ---------------------------------------------------------------------------

class ImperfectionEngine:
    """
    Adds controlled imperfection to AI suggestions
    to avoid the 'too perfect' problem (Netflix finding).
    Imperfection level increases as conversation progresses.
    """

    HESITATIONS = [
        "let me think...",
        "hmm,",
        "actually,",
        "I think...",
        "well,",
    ]

    def calibrate(self, suggestion: str, turn_count: int) -> str:
        """Apply imperfections based on conversation progression."""
        # Early = polished (first impression), later = more natural
        imperfection_level = min(0.25, turn_count * 0.015)

        if random.random() < imperfection_level:
            suggestion = self._add_hesitation(suggestion)

        if random.random() < imperfection_level * 0.4:
            suggestion = self._soften_numbers(suggestion)

        return suggestion

    def _add_hesitation(self, text: str) -> str:
        """Insert a natural hesitation point."""
        sentences = text.split(". ")
        if len(sentences) > 2:
            idx = random.randint(1, len(sentences) - 1)
            hesitation = random.choice(self.HESITATIONS)
            sentences[idx] = hesitation + " " + sentences[idx]
        return ". ".join(sentences)

    def _soften_numbers(self, text: str) -> str:
        """Replace exact numbers with approximate ranges."""
        def soften(match: re.Match) -> str:
            num = int(match.group(1))
            low = max(0, num - random.randint(5, 15))
            return f"roughly {low}-{num}%"

        return re.sub(r"(\d+)%", soften, text, count=1)
