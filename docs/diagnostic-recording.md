# Interaction audio and JSONL diagnostics

Enable recording by setting both environment variables in the gateway service and restarting it:

```sh
HERMES_VOICE_DEBUG=1
HERMES_CONVERSE_SERVER_RECORDING=1
```

Every connected session writes three files under `~/.hermes/logs/converse-recordings`:

- `input-<id>.wav`: mono PCM received by the speech detector, before server input gain.
- `input-<id>-output.wav`: mono reply PCM before transport encoding.
- `input-<id>.jsonl`: full client and server interaction frames (including text), audio chunk
  timestamps and sample offsets, and microphone RMS every 100 ms.

The `recording_started` event identifies the session, device, detector and both sample rates.
Each `audio` event gives its direction, receipt time and offset in that direction's WAV.
Reply chunks may be sent ahead of playback; their timestamps are send times, not sample end times.
WAV headers are updated on every write so open recordings play to their full written length.
`diagnostic` transcript events contain recognized user text and reply pieces independently of the
client's `transcripts` option. User transcript events include `activation.kind` (`wake` or
`speech`) and `activation.claim_id`, joining the recorded `wake` and `wake_grant` frames.
These local events are not sent to a client that did not request transcripts.
Use `offset_samples / rate` to locate a chunk in the WAV; use `received_unix_ms` to align
microphone and reply audio with the interaction frames. WAVs concatenate received chunks;
wall-clock gaps remain visible in JSONL. Reply timestamps describe server sends, not measured
speaker playback. The microphone is what the client sends after its processing; this does not
capture separate pre-AEC channels from the device.

For VAD or AEC investigation, copy all three files with the same ID, listen to the microphone
around the relevant frame, and compare it with the reply waveform and RMS timeline. Recording
continues across turns for the socket lifetime, up to 24 hours, without a 60-second cutoff.
Reconnects create a new set; `session_id` joins a resumed interaction across sets. If any active
file or the recording directory is deleted or replaced, the next recording event opens a fresh
set, recreating the directory when needed. Deleted audio is not copied into the new set.

Recording is off by default. Enable it with the consent of people whose audio and text may
be captured. Server mute closes and permanently disarms recording for the process; a restart
resets this. Remove either flag and restart to disable recording.

Files are local, owned by the service user (directory 0700, files 0600). Retain at most 20
sets; opening a new set replaces the oldest inactive set. If all sets are active or the bounded
writer queue fills, recording stops accepting data. Files older than 24 hours are removed
on startup, hourly and when opening a set, except while in use. With the service stopped,
remove retained files manually. Audio is not uploaded.
