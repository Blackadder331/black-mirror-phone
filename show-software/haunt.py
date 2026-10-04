#!/usr/bin/env python3
"""
The Haunted Phone & Mirror — show controller.

    python haunt.py                 # mode from config.yaml (sim by default)
    python haunt.py --sim --fast    # keyboard simulation, timings x0.25
    python haunt.py --real          # real hardware
    python haunt.py --list-devices  # find your USB sound cards
    python haunt.py --mic-test      # watch handset mic levels for 15 s

The whole experience is one loop:

    IDLE ─guest enters─▶ PAUSE ─▶ RINGING ─picked up─▶ CALL ─▶ THINKING ─▶ REVEAL
      ▲                                                                       │
      └── RESET ◀── room empty ◀── FIGURE ◀─stays─ LINGER ◀── GOODBYE ◀───────┘
                          ▲                          │
                          └──────────left────────────┘
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import time
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent
log = logging.getLogger("haunt")


# ── interruptions ──────────────────────────────────────────────
class HungUp(Exception):
    """The guest put the handset down."""


class RoomEmptied(Exception):
    """Everyone left before the phone was answered."""


class OperatorAbort(Exception):
    """Staff pressed Reset on the operator panel."""


# ── config ─────────────────────────────────────────────────────
def load_config(path: Path, time_scale: float = 1.0) -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    if time_scale != 1.0:
        t = cfg["timings"]
        for k, v in t.items():
            t[k] = [x * time_scale for x in v] if isinstance(v, list) else v * time_scale
    cfg["_time_scale"] = time_scale
    return cfg


# ── the show ───────────────────────────────────────────────────
class Show:
    """Owns the state machine. Hardware calls on_hook / on_presence; the
    operator panel calls operator(); everything else is awaited from run()."""

    def __init__(self, cfg, hardware, audio, transcriber, oracle, mirror):
        self.cfg, self.t = cfg, cfg["timings"]
        self.hw, self.audio, self.stt, self.oracle, self.mirror = (
            hardware, audio, transcriber, oracle, mirror)
        self.keep_transcripts = cfg.get("privacy", {}).get("keep_transcripts", False)

        # live inputs
        self.hook_up = False          # handset lifted?
        self.raw_present = False      # what the sensor says right now
        self.occupied = False         # debounced: someone is in the room
        # show state
        self.state, self.state_since = "starting", time.time()
        self.history: list[str] = []  # recent states, newest last (handy for debugging)
        self.paused = False
        self.alert: dict | None = None
        self.last: dict = {}          # last fate etc. for the operator panel
        self.shows_run = 0

        self._served = False          # this occupancy already got its show
        self._manual = False          # show was started from the panel
        self._force_reset = False
        self._start_requested = False
        self._empty_timer = None
        self._hook_up_evt = asyncio.Event()
        self._hook_down_evt = asyncio.Event()
        self._hook_down_evt.set()
        self._empty_evt = asyncio.Event()
        self._empty_evt.set()
        self._abort_evt = asyncio.Event()

    # ── inputs from hardware (called on the event loop) ──
    def on_hook(self, up: bool) -> None:
        if up == self.hook_up:
            return
        self.hook_up = up
        log.info("handset %s", "LIFTED" if up else "HUNG UP")
        if up:
            self._hook_up_evt.set()
            self._hook_down_evt.clear()
        else:
            self._hook_down_evt.set()
            self._hook_up_evt.clear()
            self.audio.stop("handset")          # hanging up always silences the earpiece

    def on_presence(self, present: bool) -> None:
        self.raw_present = present
        if present:
            if self._empty_timer:
                self._empty_timer.cancel()
                self._empty_timer = None
            if not self.occupied:
                self.occupied = True
                self._empty_evt.clear()
                log.info("room OCCUPIED")
        elif self.occupied and self._empty_timer is None:
            self._empty_timer = asyncio.get_running_loop().call_later(
                self.t["empty_room_confirm_s"], self._confirm_empty)

    def _confirm_empty(self) -> None:
        self._empty_timer = None
        self.occupied = False
        self._served = False
        self._empty_evt.set()
        log.info("room EMPTY")

    # ── operator panel (called on the event loop) ──
    def operator(self, action: str) -> str:
        t = self.t
        if action == "start":
            self._manual = self._start_requested = True
        elif action == "reset":
            self._force_reset = True
            self._abort_evt.set()
        elif action == "pause":
            self.paused = True
        elif action == "resume":
            self.paused = False
        elif action == "mute":
            self.audio.muted = True
        elif action == "unmute":
            self.audio.muted = False
        elif action == "ring_test":
            if self.state != "idle":
                return "ring test only works while idle"
            asyncio.get_running_loop().create_task(self._ring_test())
        elif action == "text_test":
            self.mirror.show_text("Your fate is sealed.", t["text_fade_in_s"],
                                  t["text_hold_s"], t["text_fade_out_s"])
        elif action == "figure_test":
            self.mirror.figure_in(min(t["figure_fade_in_s"], 5))
        elif action == "black":
            self.mirror.black()
        elif action == "clear_alert":
            self.alert = None
        else:
            return f"unknown action {action!r}"
        log.info("operator: %s", action)
        return "ok"

    def status(self) -> dict:
        return {
            "state": self.state, "since": self.state_since, "hook_up": self.hook_up,
            "present": self.raw_present, "occupied": self.occupied, "paused": self.paused,
            "muted": getattr(self.audio, "muted", False), "alert": self.alert,
            "last": self.last, "shows_run": self.shows_run,
            "hardware": getattr(self.hw, "health", lambda: "ok")(),
        }

    # ── main loop ──
    async def run(self) -> None:
        await self._safe_state()
        while True:
            await self._wait_for_guest()
            self._abort_evt.clear()
            self._force_reset = False
            self._served = True
            self.shows_run += 1
            try:
                await self._encounter()
            except RoomEmptied:
                log.info("room emptied before the call — cancelled quietly")
            except OperatorAbort:
                log.info("show aborted by operator")
            except Exception:  # never let one bad show stop the room
                log.exception("show crashed — resetting")
            finally:
                await self._safe_state()
                self._manual = False
            await self._wait_for_reset()

    async def _wait_for_guest(self) -> None:
        self._set_state("idle")
        while True:
            if self._start_requested:
                self._start_requested = False
                log.info("show started from operator panel")
                return
            if self.occupied and not self._served and not self.paused:
                if self.hook_up:                 # handset was left off by someone
                    await self._nag_until_hung_up()
                    self._set_state("idle")
                    continue
                return
            await asyncio.sleep(0.05)

    async def _wait_for_reset(self) -> None:
        self._set_state("waiting for room to empty")
        while not self._force_reset:
            if self.hook_up:
                await self._nag_until_hung_up()
                self._set_state("waiting for room to empty")
                continue
            if not self.occupied:
                break
            await asyncio.sleep(0.05)
        if self._force_reset:
            self._force_reset = False
            return                               # staff reset: skip cooldown
        self._set_state("cooldown")
        await asyncio.sleep(self.t["cooldown_after_reset_s"])

    # ── one guest's encounter ──
    async def _encounter(self) -> None:
        t = self.t
        watch_empty = not self._manual          # a manual start ignores the sensor

        # 1 ─ the uncomfortable pause (picking up early skips the ring)
        self._set_state("pause")
        picked_up = await self._wait_or_timeout(
            self._hook_up_evt.wait(), random.uniform(*t["pause_before_ring_s"]),
            on_empty=watch_empty)

        # 2 ─ ring until answered or given up
        if not picked_up:
            self._set_state("ringing")
            self.hw.set_ring(True)
            try:
                picked_up = await self._wait_or_timeout(
                    self._hook_up_evt.wait(), t["ring_give_up_s"], on_empty=watch_empty)
            finally:
                self.hw.set_ring(False)
            if not picked_up:
                log.info("nobody answered")
                return

        # 3 ─ the call
        self._set_state("call")
        name = reason = question = ""
        got_question = False
        try:
            await self._wait_or_timeout(self.audio.play("static", loop=True),
                                        t["pickup_silence_s"], on_hangup=True)
            name_job = self._transcribe_later(await self._ask("L01", t["name_listen_max_s"]))
            reason_job = self._transcribe_later(await self._ask("L02", t["reason_listen_max_s"]))
            question = await self._transcribe(await self._ask("L03", t["question_listen_max_s"]))
            if len(question.split()) < 2:        # silence or a mumble: one more chance
                question = await self._transcribe(await self._ask("L04", t["question_listen_max_s"]))
            got_question = True
            name = self.oracle.extract_name(await name_job)
            reason = await reason_job
        except HungUp:
            log.info("guest hung up mid-call")

        if got_question:
            # 4 ─ thinking: the filler line hides the AI's delay
            self._set_state("thinking")
            filler = (asyncio.create_task(self.audio.play("L05", loop=True))
                      if self.hook_up else None)
            try:
                fate, _ = await self._guard(asyncio.gather(
                    self.oracle.answer(question, name=name, reason=reason),
                    asyncio.sleep(t["filler_min_s"])))
            finally:
                if filler:
                    filler.cancel()
                    await asyncio.gather(filler, return_exceptions=True)
            self._record_fate(question, name, fate)

            # 5 ─ "Your fate is sealed…" then the mirror speaks
            if self.hook_up:
                await self._guard(self.audio.play("L06"))
            self._set_state("reveal")
            self.mirror.show_text(fate.text, t["text_fade_in_s"], t["text_hold_s"],
                                  t["text_fade_out_s"])
            await self._guard(asyncio.sleep(
                t["text_fade_in_s"] + t["text_hold_s"] + t["text_fade_out_s"]))
            await self._guard(asyncio.sleep(t["after_reveal_pause_s"]))

        # 6 ─ goodbye from the room, not the phone
        self._set_state("goodbye")
        await self._guard(self.audio.play("L07"))

        # 7 ─ leave… or stay and see it
        self._set_state("linger")
        await self._guard(asyncio.sleep(t["linger_before_figure_s"]))
        if not (self.occupied and self.raw_present) and not self._manual:
            return
        self._set_state("figure")
        self.mirror.figure_in(t["figure_fade_in_s"])
        await self._guard(self._empty_evt.wait())
        self.mirror.cut()
        log.info("they finally left")

    # ── helpers ──
    async def _ask(self, line: str, listen_max: float):
        """Speak a line on the handset, then listen. Raises HungUp."""
        await self._guard(self.audio.play(line), on_hangup=True)
        self._set_state(f"call: listening after {line}")
        clip = await self._guard(self.audio.listen(listen_max), on_hangup=True)
        self._set_state("call")
        return clip

    def _transcribe_later(self, clip) -> asyncio.Task:
        return asyncio.create_task(self._transcribe(clip))

    async def _transcribe(self, clip) -> str:
        if clip is None:
            return ""
        try:
            text = (await self.stt.transcribe(clip)).strip()
        except Exception:
            log.exception("speech-to-text failed")
            return ""
        log.info("heard: %s", text if self.keep_transcripts else f"<{len(text.split())} words>")
        return text

    def _record_fate(self, question: str, name: str, fate) -> None:
        log.info("fate (%s): %s", fate.source, fate.text)
        self.last = {"fate": fate.text, "source": fate.source, "at": time.time()}
        if self.keep_transcripts:
            self.last.update(question=question, name=name)
        if fate.alert:
            self.alert = {"at": time.time(), "message": "A guest's question mentioned self-harm. Please check in."}
            log.warning("ALERT: self-harm language in a question — staff notified")

    async def _guard(self, aw, *, on_hangup=False, on_empty=False):
        """Await `aw`, but raise as soon as an interruption happens."""
        return await self._race(aw, None, on_hangup, on_empty)

    async def _wait_or_timeout(self, aw, timeout, *, on_hangup=False, on_empty=False) -> bool:
        """True if `aw` finished, False if the timeout came first."""
        try:
            await self._race(aw, timeout, on_hangup, on_empty)
            return True
        except asyncio.TimeoutError:
            return False

    async def _race(self, aw, timeout, on_hangup, on_empty):
        main = asyncio.ensure_future(aw)
        watchers = {asyncio.ensure_future(self._abort_evt.wait()): OperatorAbort}
        if on_hangup:
            watchers[asyncio.ensure_future(self._hook_down_evt.wait())] = HungUp
        if on_empty:
            watchers[asyncio.ensure_future(self._empty_evt.wait())] = RoomEmptied
        try:
            done, _ = await asyncio.wait({main, *watchers}, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if main in done:
                return main.result()
            for w, exc in watchers.items():
                if w in done:
                    raise exc()
            raise asyncio.TimeoutError()
        finally:
            pending = [f for f in (main, *watchers) if not f.done()]
            for f in pending:
                f.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _nag_until_hung_up(self) -> None:
        self._set_state("handset off the hook")
        nag = asyncio.create_task(self.audio.play("L08", loop=True, gap_s=self.t["offhook_nag_gap_s"]))
        try:
            while self.hook_up and not self._force_reset:
                await asyncio.sleep(0.05)
        finally:
            nag.cancel()
            await asyncio.gather(nag, return_exceptions=True)

    async def _ring_test(self) -> None:
        self.hw.set_ring(True)
        await asyncio.sleep(6)
        self.hw.set_ring(False)

    async def _safe_state(self) -> None:
        self.hw.set_ring(False)
        self.audio.stop_all()
        self.mirror.black()

    def _set_state(self, state: str) -> None:
        if state != self.state:
            log.info("── %s", state.upper())
            self.state, self.state_since = state, time.time()
            self.history = (self.history + [state])[-200:]


# ── wiring it together ─────────────────────────────────────────
async def amain(cfg: dict) -> None:
    from oracle import Oracle
    from web import MirrorChannel, WebServer

    sim = cfg["mode"] == "sim"
    oracle = Oracle(cfg, BASE)
    mirror = MirrorChannel(echo=sim)

    if sim:
        from hardware import SimConsole, SimHardware
        from audio import SimAudio
        from stt import SimTranscriber
        console = SimConsole()
        audio, stt = SimAudio(cfg, console), SimTranscriber()
    else:
        from audio import RealAudio
        from stt import WhisperTranscriber
        audio, stt = RealAudio(cfg, BASE), WhisperTranscriber(cfg)

    show = Show(cfg, None, audio, stt, oracle, mirror)
    if sim:
        show.hw = SimHardware(console, show)
    else:
        from hardware import PicoHardware
        show.hw = PicoHardware(cfg, show.on_hook, show.on_presence)

    loop = asyncio.get_running_loop()
    WebServer(cfg, BASE, mirror, show, loop).start()
    await show.hw.start()
    if sim:
        console.start(loop, show)
    loop.create_task(oracle.warm_up())
    await show.run()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=BASE / "config.yaml")
    ap.add_argument("--sim", action="store_true", help="force keyboard simulation")
    ap.add_argument("--real", action="store_true", help="force real hardware")
    ap.add_argument("--fast", action="store_true", help="run all timings at 1/4 length")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--mic-test", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(args.config, 0.25 if args.fast else 1.0)
    if args.sim:
        cfg["mode"] = "sim"
    if args.real:
        cfg["mode"] = "real"

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    if args.mic_test:
        from audio import RealAudio
        RealAudio(cfg, BASE).mic_test(15)
        return

    try:
        asyncio.run(amain(cfg))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nGoodbye now.")


if __name__ == "__main__":
    main()
