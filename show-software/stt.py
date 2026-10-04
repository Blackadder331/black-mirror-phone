"""Speech-to-text. `await transcribe(clip) -> str`."""
from __future__ import annotations

import asyncio
import logging
import re
import threading

log = logging.getLogger("stt")

# Whisper sometimes "hears" these in noise or very short clips.
HALLUCINATIONS = {"you", "thank you", "thank you.", "thanks for watching", "thanks for watching!",
                  "bye", "bye.", "."}


class WhisperTranscriber:
    def __init__(self, cfg):
        from faster_whisper import WhisperModel
        s = cfg["stt"]
        log.info("loading Whisper model %s (first run downloads it)…", s["model"])
        self.model = WhisperModel(s["model"], device=s.get("device", "cpu"),
                                  compute_type=s.get("compute_type", "int8"))
        self._lock = threading.Lock()   # one transcription at a time
        log.info("Whisper ready")

    async def transcribe(self, audio) -> str:
        return await asyncio.to_thread(self._run, audio)

    def _run(self, audio) -> str:
        with self._lock:
            segments, _ = self.model.transcribe(
                audio, language="en", beam_size=1, vad_filter=False,
                condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segments).strip()
        if text.lower() in HALLUCINATIONS:
            return ""
        return re.sub(r"\s+", " ", text)


class SimTranscriber:
    async def transcribe(self, clip) -> str:
        return clip.text
