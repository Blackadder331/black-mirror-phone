"""
Scenario tests: every path a guest can take through the room, at 1/100 speed-up.
Run:  python -m unittest discover -s tests -v
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audio import SimAudio                     # noqa: E402
from haunt import Show, load_config            # noqa: E402
from oracle import Oracle                      # noqa: E402
from stt import SimTranscriber                 # noqa: E402

SCALE = 0.01


class QuietConsole:
    def __init__(self):
        self.speech = asyncio.Queue()
        self.listening = False
        self.said = []

    def say(self, text):
        self.said.append(text)


class FakeHardware:
    def __init__(self):
        self.ring_log = []

    def set_ring(self, on):
        if not self.ring_log or self.ring_log[-1] != on:
            self.ring_log.append(on)

    @property
    def rang(self):
        return True in self.ring_log


class FakeMirror:
    def __init__(self):
        self.log = []

    def show_text(self, text, *_):
        self.log.append(("text", text))

    def figure_in(self, _):
        self.log.append(("figure_in",))

    def cut(self):
        self.log.append(("cut",))

    def black(self):
        self.log.append(("black",))

    def has(self, cmd):
        return any(e[0] == cmd for e in self.log)

    def texts(self):
        return [e[1] for e in self.log if e[0] == "text"]


def make_show(generate=None):
    cfg = load_config(ROOT / "config.yaml", SCALE)
    cfg["oracle"]["name_prefix_chance"] = 0.0
    console = QuietConsole()
    audio = SimAudio(cfg, console, flush_stale_speech=False)
    gen = generate or (lambda system, prompt, temperature, timeout: "The door you opened cannot close.")
    oracle = Oracle(cfg, ROOT, generate=gen)
    hw, mirror = FakeHardware(), FakeMirror()
    show = Show(cfg, hw, audio, SimTranscriber(), oracle, mirror)
    return show, hw, mirror, console


class Scenario(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.show, self.hw, self.mirror, self.console = make_show()
        self.task = asyncio.create_task(self.show.run())
        await self.until(lambda: self.show.state == "idle")

    async def asyncTearDown(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def until(self, cond, timeout=3.0, msg=""):
        end = asyncio.get_running_loop().time() + timeout
        while not cond():
            if asyncio.get_running_loop().time() > end:
                self.fail(f"timed out waiting: {msg} (state={self.show.state})")
            await asyncio.sleep(0.005)

    def say(self, *phrases):
        for p in phrases:
            self.console.speech.put_nowait(p)

    def played(self, line_id):
        text = self.show.cfg["lines"][line_id]["text"]
        return any(text in s for s in self.console.said)

    async def walk_in_and_answer(self, *speech):
        self.say(*speech)
        self.show.on_presence(True)
        await self.until(lambda: self.show.state == "ringing", msg="ringing")
        self.show.on_hook(True)

    # ── the main paths ──
    async def test_guest_asks_then_leaves_room_resets(self):
        await self.walk_in_and_answer("Sarah", "I was curious", "Will I find love")
        await self.until(lambda: self.show.state == "linger", msg="linger")
        self.assertEqual(self.mirror.texts(), ["The door you opened cannot close."])
        for line in ("L01", "L02", "L03", "L05", "L06", "L07"):
            self.assertTrue(self.played(line), line)
        self.show.on_hook(False)
        self.show.on_presence(False)
        await self.until(lambda: self.show.state == "idle", msg="back to idle")
        self.assertFalse(self.mirror.has("figure_in"))
        self.assertEqual(self.hw.ring_log, [False, True, False])

    async def test_guest_stays_figure_appears_then_cuts_when_they_leave(self):
        await self.walk_in_and_answer("Tom", "a dare", "Is this house haunted")
        await self.until(lambda: self.show.state == "figure", msg="figure")
        self.assertTrue(self.mirror.has("figure_in"))
        self.show.on_hook(False)
        self.show.on_presence(False)
        await self.until(lambda: self.mirror.has("cut"), msg="cut")
        await self.until(lambda: self.show.state == "idle")

    async def test_picking_up_before_it_rings_skips_the_ring(self):
        self.say("Ana", "fun", "What happens next")
        self.show.on_presence(True)
        await self.until(lambda: self.show.state == "pause")
        self.show.on_hook(True)
        await self.until(lambda: self.show.state == "linger")
        self.assertFalse(self.hw.rang)
        self.assertEqual(len(self.mirror.texts()), 1)

    # ── edge cases ──
    async def test_nobody_answers_stops_ringing_and_does_not_rering_same_group(self):
        self.show.on_presence(True)
        await self.until(lambda: self.show.state == "ringing")
        await self.until(lambda: self.show.state == "waiting for room to empty", msg="gave up")
        self.assertEqual(self.hw.ring_log[-1], False)
        await asyncio.sleep(0.5)                               # still in the room…
        self.assertEqual(self.show.state, "waiting for room to empty")
        self.assertEqual(self.hw.ring_log.count(True), 1)      # …and never rang again
        self.show.on_presence(False)
        await self.until(lambda: self.show.state == "idle")

    async def test_hang_up_mid_call_skips_to_goodbye(self):
        self.show.on_presence(True)
        await self.until(lambda: self.show.state == "ringing")
        self.show.on_hook(True)
        await self.until(lambda: self.show.state.startswith("call: listening"), msg="listening")
        self.show.on_hook(False)
        await self.until(lambda: "goodbye" in self.show.history, msg="goodbye")
        self.assertEqual(self.mirror.texts(), [])
        self.assertFalse(self.played("L06"))

    async def test_silent_guest_gets_second_chance_then_prewritten_fate(self):
        self.show.on_presence(True)
        await self.until(lambda: self.show.state == "ringing")
        self.show.on_hook(True)                               # never says anything
        await self.until(lambda: "reveal" in self.show.history, timeout=5, msg="reveal")
        self.assertTrue(self.played("L04"))
        self.assertEqual(self.show.last["source"], "fallback")
        self.assertIn(self.mirror.texts()[0], self.show.oracle.fates)

    async def test_room_empties_during_pause_cancels_quietly(self):
        self.show.on_presence(True)
        await self.until(lambda: self.show.state == "pause")
        self.show.on_presence(False)
        await self.until(lambda: self.show.state == "idle", msg="idle")
        self.assertFalse(self.hw.rang)

    async def test_handset_left_off_hook_nags_and_waits(self):
        self.show.on_hook(True)
        self.show.on_presence(True)
        await self.until(lambda: self.show.state == "handset off the hook")
        await self.until(lambda: self.played("L08"))
        self.assertFalse(self.hw.rang)
        self.show.on_hook(False)
        await self.until(lambda: self.show.state == "ringing", msg="show starts once hung up")

    async def test_guest_leaves_with_handset_off_hook_no_reset_until_hung_up(self):
        await self.walk_in_and_answer("Lee", "bored", "Who lived here before")
        await self.until(lambda: self.show.state == "linger")
        self.show.on_presence(False)                          # walks out, handset dangling
        await self.until(lambda: self.show.state == "handset off the hook", msg="nag")
        await asyncio.sleep(0.2)
        self.assertNotEqual(self.show.state, "idle")
        self.show.on_hook(False)
        await self.until(lambda: self.show.state == "idle")

    async def test_self_harm_question_shows_safe_line_and_alerts_staff(self):
        await self.walk_in_and_answer("Sam", "I don't know", "should I kill myself")
        await self.until(lambda: self.show.state == "linger")
        self.assertEqual(self.mirror.texts(), [self.show.cfg["oracle"]["safe_line"]])
        self.assertIsNotNone(self.show.alert)

    async def test_operator_reset_aborts_and_does_not_retrigger_until_empty(self):
        self.show.on_presence(True)
        await self.until(lambda: self.show.state == "ringing")
        self.show.operator("reset")
        await self.until(lambda: self.show.state == "idle", msg="idle")
        self.assertEqual(self.hw.ring_log[-1], False)
        await asyncio.sleep(0.3)
        self.assertEqual(self.show.state, "idle")             # stuck sensor can't loop the show

    async def test_operator_start_runs_show_with_no_sensor(self):
        self.say("Kim", "testing", "Does this work")
        self.show.operator("start")
        await self.until(lambda: self.show.state == "ringing")
        self.show.on_hook(True)
        await self.until(lambda: "figure" in self.show.history, msg="manual show runs to the end")


class SlowOrBrokenAI(unittest.IsolatedAsyncioTestCase):
    async def run_with(self, generate):
        show, hw, mirror, console = make_show(generate)
        task = asyncio.create_task(show.run())
        for p in ("Ray", "luck", "Will I be rich"):
            console.speech.put_nowait(p)
        await asyncio.sleep(0.05)
        show.on_presence(True)
        while show.state != "ringing":
            await asyncio.sleep(0.005)
        show.on_hook(True)
        for _ in range(1000):
            if show.state == "linger":
                break
            await asyncio.sleep(0.005)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return show, mirror

    async def test_ai_too_slow_uses_fallback(self):
        import time
        show, mirror = await self.run_with(lambda *a: time.sleep(1) or "Too late.")
        self.assertEqual(show.last["source"], "fallback")

    async def test_ai_breaks_character_uses_fallback(self):
        show, mirror = await self.run_with(lambda *a: "As an AI language model I cannot predict the future.")
        self.assertEqual(show.last["source"], "fallback")

    async def test_ai_server_down_uses_fallback(self):
        def down(*a):
            raise ConnectionRefusedError("no ollama")
        show, mirror = await self.run_with(down)
        self.assertEqual(show.last["source"], "fallback")
        self.assertEqual(len(mirror.texts()), 1)


class OracleChecks(unittest.TestCase):
    def setUp(self):
        cfg = load_config(ROOT / "config.yaml")
        self.o = Oracle(cfg, ROOT, generate=lambda *a: "")

    def test_clean_keeps_one_short_sentence(self):
        self.assertEqual(self.o.clean('"The cold remembers you. It always will."'), "The cold remembers you.")
        self.assertEqual(self.o.clean("Answer: soon, very soon"), "Soon, very soon.")

    def test_clean_rejects_bad_output(self):
        self.assertIsNone(self.o.clean("Yes."))                                   # too short
        self.assertIsNone(self.o.clean(" ".join(["dark"] * 20)))                   # too long
        self.assertIsNone(self.o.clean("Sorry, I can't answer that question."))    # broke character
        self.assertIsNone(self.o.clean("Your fate is shit and always was."))       # blocklist

    def test_extract_name(self):
        e = self.o.extract_name
        self.assertEqual(e("My name is Sarah."), "Sarah")
        self.assertEqual(e("um, it's Marcus"), "Marcus")
        self.assertEqual(e("Priya"), "Priya")
        self.assertEqual(e("I'm scared"), "")
        self.assertEqual(e(""), "")

    def test_concerning(self):
        self.assertTrue(self.o.is_concerning("Should I end it all?"))
        self.assertFalse(self.o.is_concerning("Will my cat die of old age happily?"))


if __name__ == "__main__":
    unittest.main()
