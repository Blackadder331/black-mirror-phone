"""
The oracle: turns a guest's question into one short, cryptic sentence.

    fate = await oracle.answer(question, name="Sarah", reason="curiosity")
    fate.text    -> "Sarah. What you buried has learned to dig."
    fate.source  -> "ai" | "fallback" | "safe"
    fate.alert   -> True if the question mentioned self-harm (staff get notified)

It never raises and never returns something long, broken or off-script:
every AI answer is checked, and anything that fails falls back to fates.txt.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("oracle")

SYSTEM_PROMPT = """You are an ancient spirit trapped inside a mirror in an old house.
A visitor has asked you a question over a telephone.
Reply with ONE cryptic, unsettling sentence of 4 to 12 words.
Be poetic, ambiguous and quietly ominous. Never cheerful, never jokey.
Never mention AI, technology, or that you are playing a role.
Never give real medical, legal, financial or safety advice.
Never predict death, illness or harm for a real, named person.
No profanity, nothing sexual, nothing about appearance, race, religion or identity.
Keep it PG-13.
If the question is unclear, speak about the visitor instead.
Output only the sentence, with no quotation marks."""

# Questions that skip the oracle entirely and alert staff.
CONCERNING = [r"\bkill(ing)? myself\b", r"\bsuicid", r"\bwant(ing)? to die\b", r"\bend (it all|my life)\b",
              r"\bhurt(ing)? myself\b", r"\bself[- ]?harm", r"\bdon'?t want to (live|be alive|be here)\b",
              r"\bbetter off dead\b", r"\bno reason to live\b"]

# Signs the model broke character.
OFF_SCRIPT = [r"\bai\b", r"language model", r"\bas an\b", r"\bassistant\b", r"\bi can'?t\b",
              r"\bi cannot\b", r"\bsorry\b", r"\bchatbot\b", r"\bprompt\b", r"https?://"]

NAME_PATTERNS = [r"\bmy name(?: is|'s)\s+([a-z][a-z'\-]+)", r"\b(?:i am|i'm|im)\s+([a-z][a-z'\-]+)",
                 r"\b(?:it's|it is|this is)\s+([a-z][a-z'\-]+)", r"\bcall me\s+([a-z][a-z'\-]+)"]
NOT_NAMES = {"um", "uh", "hello", "hi", "hey", "yes", "yeah", "no", "the", "a", "here", "not", "nobody",
             "just", "so", "well", "okay", "ok", "oh", "me", "scared", "lost", "looking", "curious",
             "an", "your", "sorry", "fine", "good", "afraid", "i", "i'm", "im", "my", "name",
             "is", "it's", "its", "it", "this", "call", "am", "uhh", "umm", "hmm", "who", "what"}


@dataclass
class Fate:
    text: str
    source: str
    alert: bool = False


def _ollama_generate(url, model, system, prompt, temperature, max_tokens, timeout) -> str:
    body = json.dumps({
        "model": model, "system": system, "prompt": prompt, "stream": False, "keep_alive": "24h",
        "options": {"temperature": temperature, "num_predict": max_tokens, "stop": ["\n"]},
    }).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # local only, no proxies
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read())["response"]


class Oracle:
    def __init__(self, cfg, base: Path, generate=None):
        o = cfg["oracle"]
        self.o = o
        self.timeout = cfg["timings"]["oracle_timeout_s"]
        self.fates = self._load_lines(base / o["fates_file"]) or ["Not yet."]
        self.blocklist = [w.lower() for w in self._load_lines(base / o["blocklist_file"])]
        self._generate = generate or (lambda system, prompt, temperature, timeout: _ollama_generate(
            o["url"], o["model"], system, prompt, temperature, o["max_tokens"], timeout))
        self._recent: list[str] = []
        self._ai_down_until = 0.0

    @staticmethod
    def _load_lines(path: Path) -> list[str]:
        if not path.exists():
            log.warning("%s not found", path)
            return []
        return [l.strip() for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")]

    async def warm_up(self) -> None:
        """Load the model into memory before the first guest."""
        try:
            await asyncio.to_thread(self._generate, SYSTEM_PROMPT, "Question: Are you there?", 0.5, 120)
            log.info("oracle model loaded and ready")
        except Exception as e:
            log.warning("oracle not reachable (%s) — prewritten fates will be used until it is", e)

    # ── main entry ──
    async def answer(self, question: str, name: str = "", reason: str = "") -> Fate:
        try:
            return await self._answer(question, name, reason)
        except Exception:
            log.exception("oracle failed")
            return Fate(self.fallback(name), "fallback")

    async def _answer(self, question, name, reason) -> Fate:
        if self.is_concerning(question) or self.is_concerning(reason):
            return Fate(self.o["safe_line"], "safe", alert=True)
        if len(question.split()) < 2:
            return Fate(self.fallback(name), "fallback")
        if time.monotonic() < self._ai_down_until:      # don't wait on a dead server every show
            return Fate(self.fallback(name), "fallback")

        prompt = f"Visitor's name: {name or 'unknown'}\nWhy they came: {reason or 'unknown'}\nQuestion: {question}"
        deadline = time.monotonic() + self.timeout
        for temperature in (self.o["temperature"], self.o["temperature"] * 0.6):
            left = deadline - time.monotonic()
            if left <= self.timeout * 0.05:          # not worth a retry
                break
            try:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(self._generate, SYSTEM_PROMPT, prompt, temperature, left), left)
            except asyncio.TimeoutError:
                log.warning("oracle too slow")
                break
            except OSError as e:                         # server down / refused
                log.warning("oracle unreachable: %s", e)
                self._ai_down_until = time.monotonic() + 60
                break
            text = self.clean(raw)
            if text:
                return Fate(self._with_name(text, name), "ai")
            log.info("rejected oracle output: %r", raw)
        return Fate(self.fallback(name), "fallback")

    # ── checks ──
    def clean(self, raw: str) -> str | None:
        """Return one tidy sentence, or None if the output can't be used."""
        if not raw:
            return None
        text = raw.strip().strip("\"'“”‘’ ")
        text = re.sub(r"^(answer|spirit|response|oracle|fate)\s*:\s*", "", text, flags=re.I)
        text = re.split(r"(?<=[.!?…])\s+", text)[0].strip().strip("\"'“”‘’ ")
        if not text:
            return None
        if text[-1] not in ".!?…":
            text += "."
        words = text.split()
        if not (self.o["min_words"] <= len(words) <= self.o["max_words"]):
            return None
        low = text.lower()
        if any(re.search(p, low) for p in OFF_SCRIPT):
            return None
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in self.blocklist):
            return None
        return text[0].upper() + text[1:]

    @staticmethod
    def is_concerning(text: str) -> bool:
        low = (text or "").lower()
        return any(re.search(p, low) for p in CONCERNING)

    @staticmethod
    def extract_name(text: str) -> str:
        low = re.sub(r"[^a-z' \-]", " ", (text or "").lower())
        for p in NAME_PATTERNS:
            m = re.search(p, low)
            if m and m.group(1) not in NOT_NAMES:
                return m.group(1).capitalize()
        words = [w for w in low.split() if w not in NOT_NAMES]
        if 1 <= len(low.split()) <= 3 and words:      # they just said "Sarah" or "uh, Sarah"
            return words[0].capitalize()
        return ""

    def fallback(self, name: str = "") -> str:
        choices = [f for f in self.fates if f not in self._recent] or self.fates
        fate = random.choice(choices)
        self._recent = (self._recent + [fate])[-30:]
        return self._with_name(fate, name)

    def _with_name(self, text: str, name: str) -> str:
        if name and name.lower() not in text.lower() and random.random() < self.o["name_prefix_chance"]:
            return f"{name}. {text}"
        return text
