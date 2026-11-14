# Arya tones

Tones let a listener follow the voice state without seeing the device. Every
Arya client plays the same tones at the same moments. The states are defined
in [animations.md](animations.md).

| Tone | When | Sound |
|---|---|---|
| Listening | Listening starts: a matching `wake_grant`, manual Talk, or a follow-up | Rising pair: 523 Hz then 784 Hz, 70 ms each |
| Speech ended | Speech capture ends and transcription begins | Falling pair: 784 Hz then 523 Hz, 70 ms each |
| Thinking | While the state is thinking | Soft 280 ms pulse at 330 Hz, repeated at a steady interval |

## Rules

- Tones are sine notes with ramped starts and ends, so they do not click.
- Tones are quiet: information, not an event. Their volume is configurable.
- A tone is short enough that it cannot be mistaken for the assistant starting
  to speak.
- After a wake word, the listening tone plays locally as soon as the matching
  `wake_grant` arrives. It does not wait for the server. A denied or timed-out
  claim makes no sound.
- The thinking tone stops as soon as thinking ends, including on speaking,
  interruption, cancellation, or error. It never overlaps the reply.
- While muted, the listening and speech-ended tones do not play.
- A tone never delays audio processing, triggers a wake, or causes an
  interruption. If a tone cannot play, the device carries on silently.

A device without a tone generator plays an equivalent short recorded sound.
