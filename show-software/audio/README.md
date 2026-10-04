# Voice lines

Record each line with a real voice actor and save it here with exactly this file name.
WAV, any sample rate, mono or stereo (the software converts). The script text is in
`config.yaml` under `lines:` — change the words there and here together.

| File | Plays on | Line | Notes |
|---|---|---|---|
| static.wav | handset | (faint crackle) | 2–3 s, loopable, very quiet |
| L01.wav | handset | "…Who is there? Tell me your name." | |
| L02.wav | handset | "And why have you come?" | |
| L03.wav | handset | "Ask me a question." | |
| L04.wav | handset | "Speak. The living are so hard to hear." | Plays only if they said nothing |
| L05.wav | handset | (slow breathing) "…Yes… I see it…" | 4–8 s, loops while the AI thinks |
| L06.wav | handset | "Your fate is sealed. Stare into the mirror for the answer you seek." | |
| L07.wav | room | "Goodbye now." | Full range, short dark reverb |
| L08.wav | handset | (whisper) "Put it back." | Loops when the handset is left off |

Processing:

- **Handset lines**: band-limit to 300–3,400 Hz (the old-phone sound), slight pitch drop,
  peak-normalize to −3 dBFS.
- **Room line**: full range, short dark reverb, so the voice seems to have left the phone.
- The software caps earpiece level (`handset_peak_ceiling`) so a loud file can't hurt anyone.

A missing file is replaced by one second of silence and logged as an error, so the show
keeps running while you record.
