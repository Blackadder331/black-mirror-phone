# haunt-pico — firmware for the Raspberry Pi Pico (MicroPython)
#
# Copy this file to the Pico as main.py (Thonny: File > Save as > Raspberry Pi Pico).
# It reads the hook switch and presence sensor, rings the real bells, and talks
# to the brain computer over USB serial. See README for wiring.
#
# Pico → brain:  HELLO haunt-pico 1 | HOOK UP | HOOK DOWN | PRESENCE 1 | PRESENCE 0 | HB
# brain → Pico:  RING ON | RING OFF | STATUS? | PING

import sys
import select
import time
from machine import Pin

# ── wiring: change these to match your build ──────────────────
HOOK_PIN = 14          # one hook-switch contact → GP14, the other → GND
HOOK_UP_LEVEL = 1      # pin level when the handset is LIFTED (1 if the contact opens on lift)
PRESENCE_PIN = 15      # mmWave sensor OUT → GP15 (3.3 V logic)
PRESENCE_ACTIVE = 1    # sensor OUT level when someone is present
PIR_PIN = None         # optional PIR OUT pin number, or None
RING_IN1 = 16          # → H-bridge IN1
RING_IN2 = 17          # → H-bridge IN2

# ── ring behaviour ────────────────────────────────────────────
RING_HZ = 20                 # classic bell frequency
RING_ON_MS = 2000            # US cadence: 2 s ringing…
RING_OFF_MS = 4000           # …4 s silent
RING_MAX_MS = 60000          # never ring longer than this, whatever the brain says
HOST_TIMEOUT_MS = 6000       # stop ringing if the brain goes quiet
HOOK_DEBOUNCE_MS = 50
PRESENCE_DEBOUNCE_MS = 200
HEARTBEAT_MS = 2000

hook = Pin(HOOK_PIN, Pin.IN, Pin.PULL_UP)
presence = Pin(PRESENCE_PIN, Pin.IN, Pin.PULL_DOWN)
pir = Pin(PIR_PIN, Pin.IN, Pin.PULL_DOWN) if PIR_PIN is not None else None
in1 = Pin(RING_IN1, Pin.OUT, value=0)
in2 = Pin(RING_IN2, Pin.OUT, value=0)
try:
    led = Pin("LED", Pin.OUT)
except Exception:
    led = Pin(25, Pin.OUT)


def send(msg):
    sys.stdout.write(msg + "\n")


class Debounced:
    def __init__(self, read, ms):
        self.read, self.ms = read, ms
        self.state = self.cand = read()
        self.since = time.ticks_ms()

    def changed(self, now):
        v = self.read()
        if v != self.cand:
            self.cand, self.since = v, now
        elif v != self.state and time.ticks_diff(now, self.since) >= self.ms:
            self.state = v
            return True
        return False


hook_db = Debounced(lambda: hook.value() == HOOK_UP_LEVEL, HOOK_DEBOUNCE_MS)
pres_db = Debounced(lambda: presence.value() == PRESENCE_ACTIVE or (pir is not None and pir.value() == 1),
                    PRESENCE_DEBOUNCE_MS)


def report_hook():
    send("HOOK UP" if hook_db.state else "HOOK DOWN")


def report_presence():
    send("PRESENCE 1" if pres_db.state else "PRESENCE 0")


def bells_off():
    in1.value(0)
    in2.value(0)


ringing = False
ring_start = 0
flip_at = 0
polarity = 0
last_host = time.ticks_ms()
last_hb = 0
buf = ""


def handle(cmd):
    global ringing, ring_start, last_host
    last_host = time.ticks_ms()
    if cmd == "RING ON":
        if not hook_db.state and not ringing:        # never ring a lifted handset
            ringing, ring_start = True, time.ticks_ms()
    elif cmd == "RING OFF":
        ringing = False
        bells_off()
    elif cmd == "STATUS?":
        report_hook()
        report_presence()
    elif cmd in ("PING", ""):
        pass
    else:
        send("ERR " + cmd)


poll = select.poll()
poll.register(sys.stdin, select.POLLIN)
send("HELLO haunt-pico 1")
report_hook()
report_presence()

while True:
    now = time.ticks_ms()

    # commands from the brain
    while poll.poll(0):
        ch = sys.stdin.read(1)
        if ch in ("\n", "\r"):
            handle(buf.strip())
            buf = ""
        else:
            buf += ch

    # inputs
    if hook_db.changed(now):
        report_hook()
        if hook_db.state and ringing:                # lifting the handset stops the bell instantly
            ringing = False
            bells_off()
    if pres_db.changed(now):
        report_presence()

    # bells: flip polarity at 20 Hz during the "on" part of the cadence
    if ringing:
        elapsed = time.ticks_diff(now, ring_start)
        if elapsed > RING_MAX_MS or time.ticks_diff(now, last_host) > HOST_TIMEOUT_MS:
            ringing = False
            bells_off()
        elif elapsed % (RING_ON_MS + RING_OFF_MS) < RING_ON_MS:
            if time.ticks_diff(now, flip_at) >= 0:
                polarity ^= 1
                in1.value(polarity)
                in2.value(polarity ^ 1)
                flip_at = time.ticks_add(now, 1000 // (2 * RING_HZ))
        else:
            bells_off()

    # heartbeat + LED (solid while ringing, blink otherwise)
    if time.ticks_diff(now, last_hb) >= HEARTBEAT_MS:
        last_hb = now
        send("HB")
    led.value(1 if ringing else (1 if (now // 500) % 4 == 0 else 0))

    time.sleep_ms(1)
