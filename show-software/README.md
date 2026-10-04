# The Haunted Phone & Mirror — show software

A guest walks into a dark room, an old phone rings, a voice asks for their name and a
question, and the answer materializes in a mirror. If they stay after "Goodbye now," a
figure fades into the reflection. This folder runs all of it.

You can run the **entire interaction today with no hardware**: simulation mode fakes the
phone and sensor from your keyboard and shows the real mirror page in a browser.

```
haunted-room/
  haunt.py          the show: state machine, timings, every edge case   ← the logic
  hardware.py       Pico link (hook, bells, sensor) + keyboard simulation
  audio.py          plays recorded lines, listens to the handset mic
  stt.py            speech-to-text (faster-whisper)
  oracle.py         local AI answer + safety checks + fallbacks
  web.py            mirror page stream + operator panel (no extra libraries)
  config.yaml       every timing, device, line and model — tune here
  fates.txt         prewritten answers (used whenever the AI can't be)
  blocklist.txt     words the mirror may never show
  mirror/           index.html (the mirror), calibrate.html (near-black test)
  operator.html     staff control panel for your phone
  pico/main.py      firmware for the Raspberry Pi Pico
  audio/            your recorded voice lines go here
  deploy/           autostart for show nights
  tests/            19 scenario tests covering every path through the room
```

---

## 1. Try it now (simulation)

```bash
pip install pyyaml
python haunt.py --sim --fast        # --fast = all timings at 1/4 length
```

Open **http://localhost:8080/mirror** (the mirror) and **http://localhost:8080/** (operator
panel) in a browser, then type in the terminal:

```
:in                          guest walks in → pause → phone rings
:up                          pick up
My name is Sarah             (typed text = what the guest says)
I wanted to see the mirror
Will I ever leave this town
                             → the answer rises out of the dark on the mirror page
:down  then  :out            hang up and leave → room resets
                             (stay instead, and the figure fades in)
```

The AI needs [Ollama](https://ollama.com) running with a model pulled (`ollama pull llama3.2:3b`).
Without it, everything still works using prewritten fates.

---

## 2. How the show logic works

All of it lives in `Show._encounter()` in `haunt.py`, written top to bottom in the order
the guest experiences it. Timings come from `config.yaml`.

| # | State | What happens | Moves on when |
|---|---|---|---|
| 0 | **idle** | Mirror black, bells quiet | Sensor sees someone new |
| 1 | **pause** | Uncomfortable silence, random 8–20 s | Time's up → ring. Picked up early → straight to the call |
| 2 | **ringing** | Real bells, 2 s on / 4 s off | Picked up → call. 30 s, no answer → give up |
| 3 | **call** | Static → L01 name → L02 why → L03 question (10 s window) | Question heard. Silent/mumbled → L04, one more try |
| 4 | **thinking** | L05 breathing loops while the AI writes (min 3 s) | Fate ready (8 s max, else prewritten) |
| 5 | **reveal** | L06 "Your fate is sealed…", then the words materialize, hold, dissolve | ~14 s |
| 6 | **goodbye** | "Goodbye now." from the **room** speaker | Line ends |
| 7 | **linger** | 8 s of black | Left → reset. Still there → figure |
| 8 | **figure** | Figure fades in over 25 s, stays | Room empty → hard cut to black |
| 9 | **waiting / cooldown** | Room must be empty and handset down, then 10 s | → idle |

**Rules that make it robust**

- **Interruptions are exceptions.** Every wait is wrapped by `_guard()`, which aborts it the
  moment the guest hangs up (`HungUp`), everyone leaves before answering (`RoomEmptied`), or
  staff press Reset (`OperatorAbort`). One place handles each, so no state can get stuck.
- **Hanging up always silences the earpiece** instantly (`on_hook`).
- **One show per visit.** A group that ignored the phone can't re-trigger it by standing
  there; the room must empty first. Staff Reset follows the same rule, so a stuck sensor
  can't loop the show.
- **Any crash resets the room** to safe (bells off, audio off, mirror black) and carries on.

| Edge case | What the room does |
|---|---|
| Handset already off the hook when they enter | Whispers L08 "Put it back." until it's hung up, then starts |
| Nobody answers | Stops after 30 s; never re-rings the same group |
| Hangs up before asking | Skips to "Goodbye now." and the stay check |
| Hangs up after asking | The answer still appears in the mirror — the spirit answers anyway |
| Says nothing | L04 once, then a prewritten fate |
| AI slow, down, or breaks character | Prewritten fate (guest never notices) |
| Question mentions self-harm | Gentle safe line on the mirror, red alert on the operator panel |
| Walks out leaving the handset off | No reset until it's hung up; L08 loops; panel shows it |
| Room empties during the pause/ring | Cancels quietly |

---

## 3. What each hardware part must do

This is the contract between the physical build and the software. If each part passes its
test, the show works.

| Part | Must do | Connects to | Software sees | Test |
|---|---|---|---|---|
| **Earpiece** (handset receiver) | Play voice clearly at a comfortable level when held to the ear | Headphone-out of USB sound card **A** | Lines with `out: handset` | Speaker test from your OS; then L01 in a real run |
| **Mouthpiece** (electret mic) | Pick up normal speech from the mouthpiece; stay quiet when nobody talks | Mic-in of USB sound card **A** (the card supplies mic bias) | 16 kHz audio → speech detection → Whisper | `python haunt.py --mic-test`: speaking shows `*`, silence doesn't |
| **Hook switch** | One contact pair changes state when the handset lifts | Pico **GP14** + **GND** | `HOOK UP` / `HOOK DOWN` within 50 ms | Thonny serial console: lift/replace, watch the lines |
| **Bells** | Ring when driven with 20 Hz alternating polarity | Ringer coil's 2 leads → H-bridge **OUT1/OUT2**; H-bridge **IN1/IN2** → Pico **GP16/GP17** | `RING ON` / `RING OFF` | Operator panel → Ring bells |
| **Presence sensor** (mmWave) | OUT pin high while anyone is in the room, **including standing still** | **5 V (VBUS)**, **GND**, **OUT → GP15** | `PRESENCE 1` / `PRESENCE 0` | Stand still 60 s: stays 1. Leave: 0 within ~2 s |
| **Room speaker** | Play "Goodbye now." into the room | Line/headphone-out of USB sound card **B** → powered speaker | Lines with `out: room` | Speaker test; L07 in a run |
| **OLED TV** | Show the mirror page full-screen, portrait, always on, true black | HDMI from the brain computer | `http://localhost:8080/mirror` in kiosk mode | `/mirror/calibrate.html` in the dark room |
| **Brain computer** | Run this program, Ollama, and Chromium | Everything above via USB/HDMI | — | `python haunt.py --real` shows all devices found |

**Pico wiring**

```
                 Raspberry Pi Pico
   hook contact ──── GP14     VBUS ──── 5V  → mmWave VCC (and H-bridge +5V if needed)
   hook contact ──── GND      GND  ──── mmWave GND, H-bridge GND  (common ground!)
   mmWave OUT  ───── GP15
   H-bridge IN1 ──── GP16     H-bridge +V ── 24–35 V DC supply (not from the Pico)
   H-bridge IN2 ──── GP17     H-bridge OUT1/OUT2 ── ringer coil leads
   USB ─────────── brain computer (power + serial)
```

- If lifting the handset reads as `HOOK DOWN`, set `HOOK_UP_LEVEL = 0` at the top of
  `pico/main.py`. Pin numbers are all at the top of that file.
- L298N-type modules: above 12 V, remove the board's 5 V-enable jumper and feed its 5V pin from VBUS.
- The Pico stops the bells by itself when the handset lifts, after 60 s, or if the brain
  goes quiet for 6 s — the bells can never get stuck ringing.
- Two identical USB sound cards show up with identical names. Buy two different models, or
  put device numbers from `--list-devices` in `config.yaml`.

---

## 4. Going live

1. **Install** on the brain computer (Linux recommended; also needs the PortAudio library,
   `sudo apt install libportaudio2`):
   ```bash
   pip install -r requirements-real.txt
   ollama pull llama3.2:3b          # or any small instruct model; set it in config.yaml
   ```
2. **Record the voice lines** listed in `audio/README.md` into `audio/`.
3. **Flash the Pico**: install MicroPython on it, copy `pico/main.py` to it as `main.py`.
4. **Find your sound cards**: `python haunt.py --list-devices`, put the names in `config.yaml`.
5. **Tune the mic**: `python haunt.py --mic-test`.
6. **Set** `mode: real` in `config.yaml` and run `python haunt.py`.
7. **Open the mirror** on the TV: `deploy/kiosk.sh`. **Open the panel** on your phone:
   `http://<brain-ip>:8080/`.
8. **Calibrate the dark**: open `/mirror/calibrate.html` on the TV, follow the instructions,
   then adjust `--fig-peak` and colors at the top of `mirror/index.html`.
9. **Film the figure** and save it as `mirror/figure.mp4`. Until then a placeholder
   silhouette is used.

For show nights, `deploy/` has a systemd unit that starts everything at boot and restarts
the controller if it ever crashes.

---

## 5. Tests

```bash
python -m unittest discover -s tests -v
```

Nineteen scenarios run the real state machine at 100× speed: full visit, staying for the
figure, early pickup, no answer, hang-ups, silence, off-hook handsets, self-harm alert,
operator reset/start, slow/broken/missing AI, and the oracle's text checks. Run them after
any change to the logic.

## Privacy

Audio is never saved. Guests' words are not logged or shown unless you set
`privacy.keep_transcripts: true`. The operator panel shows only the fate.
