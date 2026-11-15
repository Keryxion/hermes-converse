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
- Do not change the level over time. No automatic gain control, compression,
  limiting, noise gate, or noise suppression that lowers speech. Each of these
  makes a voice raised over a reply look like the voice before it.
- A fixed gain is allowed when it is the same for the whole session and never
  clips. Prefer none: quiet PCM16 still carries the speech.
- Do not normalize a buffered pre-roll on its own. It is sent as it was heard,
  followed by live audio at the same level.

Streaming, buffering on a wake word, and mute are in
[the client spec](client.md#microphone-streaming-and-barge-in).

## What the server does with it

The server keeps the audio as received and derives two things from it:

| Use | Input |
|---|---|
| Speech detection, endpointing and transcription | A copy whose level is brought to what these models expect. Its gain follows the session's own level, so speech from across the room is speech. |
| Barge-in and playback echo rejection | The received audio, compared with the same session's own floor and with the level of its own reply's echo. Never the normalized copy. |

Rules:

- Speech detection does not depend on the input's absolute level. A device
  whose speech arrives 30 dB below a typical USB or ESP32 microphone gets the
  same onset, endpoint and transcript.
- The level of the normalized copy never decides an interruption. A voice
  raised over a reply stays louder than the reply's echo, by as much as it was
  louder at the microphone.
- The copy's gain changes slowly and never within an utterance, so a word is
  not louder at its end than at its start.
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
