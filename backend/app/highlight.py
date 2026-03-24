"""
Real-time suggestion highlight tracker for NoHuman teleprompter.

Matches streaming candidate speech against the current suggestion text to
determine which word the candidate is currently reading. Runs purely on
string comparison — no LLM, no API calls — so latency is effectively zero.

Handles:
- Exact word matches (case-insensitive, punctuation-stripped)
- Candidate-inserted fillers ("um", "uh", "like", "so", "basically", "you know")
- Forward jumps (candidate skips ahead in the suggestion)
- Slight paraphrasing (fuzzy single-character tolerance)
"""

import re
import unicodedata

_FILLER_WORDS = frozenset({
    "um", "uh", "erm", "hmm", "hm", "ah", "oh", "eh",
    "like", "basically", "actually", "literally", "right",
    "so", "well", "okay", "ok", "yeah", "yes", "no",
    "you know", "i mean", "sort of", "kind of",
})

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize(word: str) -> str:
    """Lowercase, strip punctuation and diacritics for comparison."""
    w = _PUNCT_RE.sub("", word.lower().strip())
    w = unicodedata.normalize("NFKD", w)
    return w


def _is_filler(word: str) -> bool:
    return _normalize(word) in _FILLER_WORDS


def _fuzzy_eq(a: str, b: str) -> bool:
    """Exact match or within 1-char edit distance (fast check)."""
    na, nb = _normalize(a), _normalize(b)
    if na == nb:
        return True
    if abs(len(na) - len(nb)) > 1:
        return False
    if len(na) == len(nb):
        return sum(ca != cb for ca, cb in zip(na, nb)) <= 1
    short, long_ = (na, nb) if len(na) < len(nb) else (nb, na)
    j = 0
    misses = 0
    for i in range(len(long_)):
        if j < len(short) and long_[i] == short[j]:
            j += 1
        else:
            misses += 1
        if misses > 1:
            return False
    return True


class SuggestionTracker:
    """
    Tracks the candidate's reading position within a suggestion.

    Usage:
        tracker = SuggestionTracker("I have eight years of experience in distributed systems")
        pos = tracker.update("I have eight years")  # returns 4
        pos = tracker.update("I have eight years of um experience")  # returns 6 (skips "um")
    """

    def __init__(self, suggestion: str):
        self._raw = suggestion
        self._words = suggestion.split()
        self._norm_words = [_normalize(w) for w in self._words]
        self._cursor = 0
        self._last_spoken_count = 0

    @property
    def word_count(self) -> int:
        return len(self._words)

    @property
    def cursor(self) -> int:
        return self._cursor

    def reset(self, suggestion: str) -> None:
        """Replace the suggestion being tracked."""
        self._raw = suggestion
        self._words = suggestion.split()
        self._norm_words = [_normalize(w) for w in self._words]
        self._cursor = 0
        self._last_spoken_count = 0

    def update(self, spoken_text: str) -> int:
        """
        Process the latest interim transcript and return the word index
        the candidate has reached in the suggestion (0-based, exclusive —
        i.e., words[0:pos] have been spoken).
        """
        if not spoken_text or not self._words:
            return self._cursor

        spoken_words = spoken_text.split()
        if not spoken_words:
            return self._cursor

        content_words = [w for w in spoken_words if not _is_filler(w)]
        if not content_words:
            return self._cursor

        if len(content_words) <= self._last_spoken_count:
            return self._cursor

        new_words = content_words[self._last_spoken_count:]
        self._last_spoken_count = len(content_words)

        for spoken_w in new_words:
            matched = False

            if self._cursor < len(self._norm_words):
                if _fuzzy_eq(spoken_w, self._words[self._cursor]):
                    self._cursor += 1
                    matched = True

            if not matched:
                scan_limit = min(self._cursor + 15, len(self._norm_words))
                for j in range(self._cursor + 1, scan_limit):
                    if _fuzzy_eq(spoken_w, self._words[j]):
                        self._cursor = j + 1
                        matched = True
                        break

        return self._cursor

    def get_position_ratio(self) -> float:
        """Return progress as 0.0-1.0."""
        if not self._words:
            return 0.0
        return min(self._cursor / len(self._words), 1.0)
