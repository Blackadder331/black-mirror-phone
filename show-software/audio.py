"""
Audio: play the recorded lines and listen to the handset microphone.

Both classes share one interface, which is all the show uses:

    await play(line_id, loop=False, gap_s=0)  plays on that line's output
                                              ("handset" or "room"); returns when
                                              finished or when stop() is called;
                                              cancelling the task stops it too
    await listen(max_s)  -> clip | None       waits for speech, returns when they
                                              stop talking; None if they never spoke
    stop(output) / stop_all()                 silence an output immediately
    muted                                     True = everything plays silently
"""
from __future__ import annotations

import asyncio
import logging
import queue
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("audio")
OUTPUTS = ("handset", "room")


class _Interrupts:
    """One asyncio.Event per output; stop() fires it and arms a fresh one."""

    def __init__(self):
        self._evt = {o: asyncio.Event() for o in OUTPUTS}

    def current(self, out: str) -> asyncio.Event:
        return self._evt[out]

    def fire(self, out: str) -> None:
        self._evt[out].set()
        self._evt[out] = asyncio.Event()


async def _until(evt: asyncio.Event, seconds: float) -> bool:
    """Sleep `seconds` unless `evt` fires first. True if interrupted."""
    try:
        await asyncio.wait_for(evt.wait(), seconds)
        return True
    except asyncio.TimeoutError:
        return False


# ── simulation ─────────────────────────────────────────────────
@dataclass
class SimClip:
    text: str


class SimAudio:
    """Prints each line instead of playing it; typed text stands in for speech."""

    def __init__(self, cfg, console, flush_stale_speech: bool = True):
        self.lines = cfg["lines"]
        self.console = console
        self.scale = cfg.get("_time_scale", 1.0)
        self.flush = flush_stale_speech
        self.muted = False
        self._stop = _Interrupts()

    def _duration(self, text: str) -> float:
        return max(0.8, 0.38 * len(text.split())) * self.scale

    async def play(self, line_id: str, loop: bool = False, gap_s: float = 0) -> None:
        line = self.lines[line_id]
        out, text = line["out"], line["text"]
        stop = self._stop.current(out)
        icon = "📞" if out == "handset" else "🔈"
        while True:
            if not self.muted:
                self.console.say(f"  {icon} [{out}] {text}")
            if await _until(stop, self._duration(text)):
                return
            if not loop:
                return
            if gap_s and await _until(stop, gap_s):
                return

    async def listen(self, max_s: float):
        q = self.console.speech
        if self.flush:
            while not q.empty():
                q.get_nowait()
        self.console.listening = True
        self.console.say(f"  🎤 (the line is listening — type what the guest says, {max_s:.0f}s)")
        try:
            return SimClip(await asyncio.wait_for(q.get(), max_s))
        except asyncio.TimeoutError:
            self.console.say("  🎤 (…silence)")
            return None
        finally:
            self.console.listening = False

    def stop(self, out: str) -> None:
        self._stop.fire(out)

    def stop_all(self) -> None:
        for o in OUTPUTS:
            self.stop(o)


# ── real sound cards ───────────────────────────────────────────
def _find_device(sd, wanted, kind: str) -> int:
    """`wanted` = device index or a case-insensitive substring of its name."""
    key = "max_output_channels" if kind == "output" else "max_input_channels"
    devices = sd.query_devices()
    if isinstance(wanted, int) or (isinstance(wanted, str) and wanted.isdigit()):
        return int(wanted)
    for i, d in enumerate(devices):
        if wanted.lower() in d["name"].lower() and d[key] > 0:
            return i
    names = [d["name"] for d in devices if d[key] > 0]
    raise RuntimeError(f"No {kind} device matching {wanted!r}. Available: {names}")


def _resample(x, src: int, dst: int):
    import numpy as np
    if src == dst or len(x) == 0:
        return x.astype("float32")
    if src % dst == 0:                       # e.g. 48k → 16k: average blocks (cheap low-pass)
        k = src // dst
        x = x[: len(x) - len(x) % k]
        return x.reshape(-1, k).mean(axis=1).astype("float32")
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype("float32")


class RealAudio:
    MIC_RATE = 16000          # what Whisper and the VAD want
    FRAME_MS = 30

    def __init__(self, cfg, base: Path):
        import numpy as np
        import sounddevice as sd
        import soundfile as sf
        self.np, self.sd = np, sd
        a = cfg["audio"]
        self.t = cfg["timings"]
        self.muted = False
        self._stop = _Interrupts()
        self.gain = {"handset": a["handset_gain"], "room": a["room_gain"]}
        self.ceiling = a["handset_peak_ceiling"]
        self.dev = {"handset": _find_device(sd, a["handset_device"], "output"),
                    "room": _find_device(sd, a["room_device"], "output")}
        self.mic = _find_device(sd, a.get("mic_device") or a["handset_device"], "input")
        self.rate, self.channels = {}, {}
        for out, idx in self.dev.items():
            info = sd.query_devices(idx)
            self.rate[out] = int(info["default_samplerate"])
            self.channels[out] = min(2, info["max_output_channels"])
            log.info("%s output → [%d] %s @ %d Hz", out, idx, info["name"], self.rate[out])
        log.info("microphone → [%d] %s", self.mic, sd.query_devices(self.mic)["name"])

        # load every line into memory, mono, at its device's rate
        self.clips = {}
        for line_id, line in cfg["lines"].items():
            out, path = line["out"], base / a["audio_dir"] / line["file"]
            try:
                data, sr = sf.read(path, dtype="float32", always_2d=True)
                self.clips[line_id] = (out, _resample(data.mean(axis=1), sr, self.rate[out]))
            except Exception as e:
                log.error("missing/unreadable %s (%s) — using 1 s of silence", path, e)
                self.clips[line_id] = (out, np.zeros(self.rate[out], dtype="float32"))

        self.vad = self._make_vad(a)

    # ── playback ──
    async def play(self, line_id: str, loop: bool = False, gap_s: float = 0) -> None:
        np, sd = self.np, self.sd
        out, samples = self.clips[line_id]
        rate = self.rate[out]
        data = samples * (0.0 if self.muted else self.gain[out])
        if out == "handset":
            data = np.clip(data, -self.ceiling, self.ceiling)
        if loop and gap_s:
            data = np.concatenate([data, np.zeros(int(gap_s * rate), dtype="float32")])

        aio = asyncio.get_running_loop()
        finished = aio.create_future()
        stop = self._stop.current(out)
        pos = 0

        def callback(outdata, frames, _time, _status):
            nonlocal pos
            if loop:                                     # wrap around forever
                chunk = data[(pos + np.arange(frames)) % len(data)]
                pos = (pos + frames) % len(data)
            else:
                chunk = data[pos:pos + frames]
                pos += frames
            n = len(chunk)
            outdata[:n] = chunk[:, None]
            if n < frames:
                outdata[n:] = 0
                raise sd.CallbackStop

        def on_finished():
            aio.call_soon_threadsafe(lambda: finished.done() or finished.set_result(None))

        stream = sd.OutputStream(device=self.dev[out], samplerate=rate, channels=self.channels[out],
                                 dtype="float32", callback=callback, finished_callback=on_finished)
        stopper = asyncio.ensure_future(stop.wait())
        try:
            stream.start()
            await asyncio.wait({finished, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
            if not finished.done():
                stream.abort()
            stream.close()

    def stop(self, out: str) -> None:
        self._stop.fire(out)

    def stop_all(self) -> None:
        for o in OUTPUTS:
            self.stop(o)

    # ── listening ──
    def _make_vad(self, a):
        mode = a.get("vad", "auto")
        if mode in ("auto", "webrtc"):
            try:
                import webrtcvad
                vad = webrtcvad.Vad(int(a.get("vad_aggressiveness", 2)))
                log.info("voice detection: WebRTC VAD")
                return lambda f: vad.is_speech((f * 32767).astype("int16").tobytes(), self.MIC_RATE)
            except ImportError:
                if mode == "webrtc":
                    raise
        thr = a.get("energy_threshold", 0.015)
        log.info("voice detection: energy threshold %.3f", thr)
        return lambda f: float(self.np.sqrt(self.np.mean(f ** 2))) > thr

    def _open_mic(self, q: queue.Queue):
        """Prefer 16 kHz; fall back to the device's native rate and downsample."""
        sd = self.sd
        for rate in (self.MIC_RATE, int(sd.query_devices(self.mic)["default_samplerate"])):
            try:
                stream = sd.InputStream(device=self.mic, samplerate=rate, channels=1, dtype="float32",
                                        blocksize=int(rate * self.FRAME_MS / 1000),
                                        callback=lambda d, *_: q.put(d[:, 0].copy()))
                return stream, rate
            except Exception:
                continue
        raise RuntimeError("could not open the handset microphone")

    async def listen(self, max_s: float):
        np = self.np
        q: queue.Queue = queue.Queue()
        stream, rate = self._open_mic(q)
        frame_len = int(self.MIC_RATE * self.FRAME_MS / 1000)
        silence_needed = int(self.t["end_of_speech_silence_s"] * 1000 / self.FRAME_MS)
        preroll, kept, pending = [], [], np.zeros(0, dtype="float32")
        started, silent_run, speech_frames = False, 0, 0
        deadline = time.monotonic() + max_s
        with stream:
            while time.monotonic() < deadline:
                try:
                    block = q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue
                pending = np.concatenate([pending, _resample(block, rate, self.MIC_RATE)])
                while len(pending) >= frame_len:
                    frame, pending = pending[:frame_len], pending[frame_len:]
                    speech = self.vad(frame)
                    if not started:
                        preroll = (preroll + [frame])[-10:]          # keep 300 ms before speech
                        if speech:
                            started, kept = True, list(preroll)
                        continue
                    kept.append(frame)
                    speech_frames += speech
                    silent_run = 0 if speech else silent_run + 1
                    if silent_run >= silence_needed:
                        deadline = 0
                        break
        if not started or speech_frames < 8:                         # < ~0.25 s of real speech
            return None
        return np.concatenate(kept)

    def mic_test(self, seconds: int = 15) -> None:
        """Print a live level meter so you can set energy_threshold."""
        np, q = self.np, queue.Queue()
        stream, rate = self._open_mic(q)
        print(f"Speak into the handset for {seconds}s. '#' = level, '*' = counts as speech.")
        end = time.monotonic() + seconds
        with stream:
            while time.monotonic() < end:
                f = _resample(q.get(), rate, self.MIC_RATE)
                if len(f) < int(self.MIC_RATE * self.FRAME_MS / 1000):
                    continue
                rms = float(np.sqrt(np.mean(f ** 2)))
                bar = "#" * min(60, int(rms * 400))
                print(f"{rms:6.3f} {'*' if self.vad(f[:480]) else ' '} {bar}")
