# Arya tones

Tones let a listener follow the voice state without seeing the device. Every
Arya client plays the same tones at the same moments. The states are defined
in [animations.md](animations.md).

| Tone | When | Sound |
|---|---|---|
| Listening | Every time the device enters listening: a matching `wake_grant`, manual Talk, a `state` of `listening`, or a return to listening after a reply | Rising pair: 523 Hz then 784 Hz, 70 ms each |
| Thinking | Entering thinking (a `state` of `thinking`), then each repeated `thinking` frame while thinking lasts | One soft 280 ms pulse at 330 Hz |
| Listening ended | Listening ends in silence with no request (a `quiet` frame), and the device returns to standby | Falling pair: 784 Hz then 523 Hz, 70 ms each |
| Confirm | The assistant did what was asked and has nothing to say | One note: 1047 Hz, 70 ms |
| Error | The assistant could not do it | Two low notes: 262 Hz, 70 ms each, 70 ms apart |
| Approval | The assistant needs permission and is about to ask | Three rising notes: 523 Hz, 659 Hz, 784 Hz, 70 ms each |
| Announcement | An announcement is about to be spoken | Three notes: 659 Hz, 523 Hz, 784 Hz, 150 ms each |

The client plays listening, thinking, and listening ended. The server renders
confirm, error, approval, and announcement into the reply audio, so a client plays them as
reply audio and has no code for them. Each sounds where it falls in the reply:
after the speech before it and before the speech after it, never on top of
either or of another tone.

## Rules

- Tones are sine notes with ramped starts and ends, so they do not click.
- Tones are quiet: information, not an event. The client's tones have a
  configurable volume; the server's follow the reply volume.
- A tone is short enough that it cannot be mistaken for the assistant starting
  to speak.
- The announcement tone is the exception: it is as loud as the speech after it,
  so someone across the room knows to listen.
- After a wake word, the listening tone plays locally as soon as the matching
  `wake_grant` arrives. It does not wait for the server. A denied or timed-out
  claim makes no sound.
- A thinking tone still sounding when the reply starts, or on interruption,
  cancellation, or error, is cut off. It never overlaps the reply: `thinking`
  frames that arrive while the reply is playing make no sound.
- While muted, the listening and listening-ended tones do not play. Confirm,
  error, approval, and announcement are reply audio, which a muted device still plays.
- A tone never delays audio processing, triggers a wake, or causes an
  interruption. If a tone cannot play, the device carries on silently.

A device without a tone generator plays an equivalent short recorded sound.
