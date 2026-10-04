"""
Hardware links.

PicoHardware  — talks to the Raspberry Pi Pico over USB serial (pico/main.py).
SimHardware   — keyboard stands in for the hook switch, bells and sensor.

Serial protocol (one text line each way, newline-terminated):

    Pico → brain                      brain → Pico
    HELLO haunt-pico 1   (on boot)    RING ON    start the 2s-on/4s-off ring
    HOOK UP | HOOK DOWN               RING OFF   stop ringing
    PRESENCE 1 | PRESENCE 0           STATUS?    resend HOOK + PRESENCE
    HB                   (every 2 s)  PING       "brain is alive" (every 2 s)
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time

log = logging.getLogger("hardware")
PICO_USB_VID = 0x2E8A  # Raspberry Pi


class PicoHardware:
    def __init__(self, cfg, on_hook, on_presence):
        self.port_name = cfg["hardware"].get("serial_port", "auto")
        self.baud = cfg["hardware"].get("baud", 115200)
        self.on_hook, self.on_presence = on_hook, on_presence
        self._ser = None
        self._lock = threading.Lock()
        self._last_heard = 0.0
        self._loop = None

    def health(self) -> str:
        if self._ser is None:
            return "Pico not connected"
        if time.time() - self._last_heard > 6:
            return "Pico silent (no heartbeat)"
        return "ok"

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        threading.Thread(target=self._reader, daemon=True, name="pico-reader").start()
        self._loop.create_task(self._pinger())

    def set_ring(self, on: bool) -> None:
        self._send("RING ON" if on else "RING OFF")

    # ── internals ──
    def _find_port(self) -> str | None:
        if self.port_name != "auto":
            return self.port_name
        from serial.tools import list_ports
        ports = list(list_ports.comports())
        for p in ports:
            if p.vid == PICO_USB_VID:
                return p.device
        for p in ports:
            if "ACM" in p.device or "usbmodem" in p.device:
                return p.device
        return None

    def _send(self, line: str) -> None:
        with self._lock:
            if self._ser is None:
                if line != "PING":
                    log.warning("Pico not connected; dropped %r", line)
                return
            try:
                self._ser.write((line + "\n").encode())
            except Exception as e:
                log.warning("serial write failed: %s", e)

    async def _pinger(self) -> None:
        while True:
            self._send("PING")
            await asyncio.sleep(2)

    def _reader(self) -> None:
        import serial
        while True:
            port = self._find_port()
            if not port:
                log.warning("no Pico found — retrying in 3 s")
                time.sleep(3)
                continue
            try:
                ser = serial.Serial(port, self.baud, timeout=1)
                with self._lock:
                    self._ser = ser
                log.info("Pico connected on %s", port)
                self._send("STATUS?")
                while True:
                    raw = ser.readline()
                    if raw:
                        self._handle(raw.decode(errors="replace").strip())
            except Exception as e:
                log.warning("Pico link lost (%s) — reconnecting", e)
                with self._lock:
                    self._ser = None
                time.sleep(2)

    def _handle(self, line: str) -> None:
        self._last_heard = time.time()
        call = lambda fn, v: self._loop.call_soon_threadsafe(fn, v)
        if line == "HOOK UP":
            call(self.on_hook, True)
        elif line == "HOOK DOWN":
            call(self.on_hook, False)
        elif line == "PRESENCE 1":
            call(self.on_presence, True)
        elif line == "PRESENCE 0":
            call(self.on_presence, False)
        elif line.startswith("HELLO"):
            log.info("Pico says: %s", line)
            self._send("STATUS?")
        elif line and line != "HB":
            log.debug("Pico: %s", line)


# ── simulation ─────────────────────────────────────────────────
HELP = """
  ┌─ SIMULATION ───────────────────────────────────────────┐
  │  :in    guest walks in        :out   guest leaves      │
  │  :up    pick up the handset   :down  hang up           │
  │  :status  show state          :quit  stop              │
  │  anything else = what the guest says into the phone    │
  │  mirror page: http://localhost:{port}/mirror            │
  │  operator panel: http://localhost:{port}/               │
  └────────────────────────────────────────────────────────┘"""


class SimConsole:
    """Reads stdin on a thread; commands go to the show, other text is 'speech'."""

    def __init__(self):
        self.speech: asyncio.Queue[str] = asyncio.Queue()
        self.listening = False

    def start(self, loop, show) -> None:
        self.loop, self.show = loop, show
        print(HELP.format(port=show.cfg["web"]["port"]))
        threading.Thread(target=self._read, daemon=True, name="console").start()

    def say(self, text: str) -> None:
        print(text, flush=True)

    def _read(self) -> None:
        for raw in sys.stdin:
            self.loop.call_soon_threadsafe(self._handle, raw.strip())

    def _handle(self, line: str) -> None:
        cmd = line.lower()
        if not line:
            return
        if cmd == ":in":
            self.show.on_presence(True)
        elif cmd == ":out":
            self.show.on_presence(False)
        elif cmd == ":up":
            self.show.on_hook(True)
        elif cmd == ":down":
            self.show.on_hook(False)
        elif cmd == ":status":
            s = self.show.status()
            self.say(f"  state={s['state']}  hook_up={s['hook_up']}  occupied={s['occupied']}")
        elif cmd == ":quit":
            for task in asyncio.all_tasks(self.loop):
                task.cancel()
        elif cmd.startswith(":"):
            self.say("  unknown command — try :in :out :up :down :status :quit")
        elif self.listening:
            self.speech.put_nowait(line)
        else:
            self.say("  (nobody on the line is listening right now)")


class SimHardware:
    def __init__(self, console: SimConsole, show):
        self.console, self.show = console, show
        self.ringing = False

    def health(self) -> str:
        return "simulation"

    async def start(self) -> None:
        pass

    def set_ring(self, on: bool) -> None:
        if on and not self.ringing:
            self.console.say("  🔔 RRRING… RRRING…   (type :up to answer)")
        elif not on and self.ringing:
            self.console.say("  🔕 ringing stopped")
        self.ringing = on
