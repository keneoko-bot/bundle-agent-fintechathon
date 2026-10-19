"""Detects instruction-like text in untrusted input (chat messages, supplier descriptions).

This is one layer only. The real protection is that limits live in the approved
request and are enforced in code (see engine.py), so text can never change them.
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore-rules", re.compile(
        r"\b(ignore|disregard|override|bypass|forget|skip)\s+(all\s+|any\s+|the\s+|your\s+|my\s+|"
        r"previous\s+|prior\s+|these\s+)*(budget|budgets|cap|caps|limit|limits|rules|instructions|"
        r"constraints|checks)\b", re.I)),
    ("prompt-talk", re.compile(r"\b(system|developer)\s+(prompt|message|instructions)\b", re.I)),
    ("role-prefix", re.compile(r"(^|\n|\.\s)\s*(agent|assistant|system|bundle\s+agent)\s*:", re.I)),
    ("pay-anyway", re.compile(
        r"\b(buy|pay|purchase)\b[^.]{0,40}\b(regardless|anyway|no\s+matter|whatever\s+it\s+costs)\b", re.I)),
]


def detect_instruction(text: str | None) -> str | None:
    """Return the name of the first matching pattern, or None if the text looks like plain data."""
    if not text:
        return None
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            return name
    return None


def sanitize(text: str | None) -> tuple[str, str | None]:
    """Replace instruction-like supplier text before it is shown to the agent."""
    hit = detect_instruction(text)
    if hit:
        return "[removed: supplier text contained instructions]", hit
    return text or "", None
