# Server diagnostic recordings

Recording is off by default. It is enabled only when both `HERMES_VOICE_DEBUG=1` and
`HERMES_CONVERSE_SERVER_RECORDING=1` are present in the server process environment. The
existing voice debug flag alone never enables recording. Remove the recording flag and restart
the service to disable recording; leaving both flags set enables recording on later restarts.
Do not enable it without consent from people whose audio may be captured.

The recorder stores mono input PCM and a JSONL timeline with 100 ms RMS values and selected
server turn frames. Server mute permanently disarms and closes diagnostic recording for all
sessions in the process, including across unmute and reconnect; a restart resets this disarm.
It records text lengths in place of text. Each retained recording contains up to 60 seconds
of audio and 2,000 timeline events. The retained recording limit is 20 per directory across
processes. At capacity, subsequent recording requests are declined. If recording falls behind
the socket, its bounded queue stops accepting data.

The service user owns the private directory and files under `~/.hermes/logs/converse-recordings`
(directory mode 0700, files 0600). The running service checks expiration on startup, hourly,
and when opening a recording. It removes inactive diagnostic files older than 24 hours,
including old orphan sidecars and legacy captures. With the service stopped, remove retained
files manually when their retention window ends. Temporary transcription files follow the normal
capture lifecycle.
