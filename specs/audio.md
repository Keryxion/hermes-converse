# Arya audio

How microphone audio gets from a device to the server's speech detection,
endpointing, transcription and barge-in. The goal is far-field voice: a person
speaking normally from across the room, or the house, is heard the way a stock
Echo hears them, and a person who talks over a reply interrupts it.

Each device's microphone delivers speech at its own level. The same voice can
arrive 30 dB apart on two devices, and 20 dB apart on one device between up
close and across the room. Level is information, not a setting: the server uses
it to tell a voice raised over a reply from the reply's own echo. So the device
sends what its microphone hears, and the server makes its decisions not depend
on how loud that is.

## What the device sends

- Send the microphone's own output: after the device's echo cancellation and
  beamforming where it has them, as PCM16 mono at `input_rate`.
- Send it at the level it was captured. No automatic gain control, no fixed
  digital gain, no compression, limiting, noise gate, or noise suppression that
  lowers speech. Adaptive gain makes a voice raised over a reply look like the
  voice before it and lifts residual echo into false barge-in; any gain hides
  what the microphone heard. Quiet PCM16 still carries the speech.
- Allowed: echo cancellation and beamforming, format conversion that loses no
  level (sample rate, a lossless narrowing to 16 bits), and analog preamp
  settings.
- A wake word engine that wants a different level changes its own copy, never
  the audio sent upstream.

Streaming, buffering on a wake word, and mute are in
[the client spec](client.md#microphone-streaming-and-barge-in).

## What the server does with it

The server brings the session's input to the level its speech detection,
endpointing and transcription expect, on its own copy:

- One gain per device, learned from the quiet end of the room's level while
  its session is idle. A microphone whose room floor is already at a normal
  level keeps it; a quieter one is raised, up to +40 dB. The server keeps what
  it learned per `device_name`, across reconnects and its own restarts, so a
  device's first request after either is heard at its usual level.
- The gain is held while a capture, a turn, or a reply is under way. A voice
  raised over a reply stays louder than the reply's echo by as much as it was
  at the microphone, and a word is not louder at its end than at its start.
- Digital silence does not teach the gain.
- Speech detection therefore does not depend on the input's absolute level. A
  device whose speech arrives 30 dB or more below a typical USB or ESP32
  microphone gets the same onset, endpoint and transcript.
- Traces report levels as received, with the session's gain beside them
  (`input.gain_db`).
- `quiet` counts silence in the received audio. A wake activation's buffered
  pre-roll is received audio too: the wake word in it is speech, and the
  silence after it counts.

## Acceptance

With the speech corpus played at its own level, and again 30 dB and 40 dB
lower:

- each request is detected, endpointed and transcribed the same at all three
  levels;
- `quiet` does not end a request that was spoken;
- an interruption spoken over a reply interrupts it at all three levels, and
  the reply's echo alone does not.
