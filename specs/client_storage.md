# Arya iOS and macOS local conversation storage

This contract applies only to Arya iOS and macOS. It complements the transcript
and inclusive turn replay contract in [client.md](client.md); it does not
require SQLite or local message storage on ESP32, Echo, or any other client.
It specifies desired behavior, not an existing implementation.

## Storage

Store local conversation messages in SQLite, one row per local message. Each
row has at least a turn ID, a local message ID, and the JSON message. The JSON
retains the fields needed by the existing transcript and history views, including
role, text, ordering and status. The message ID is a client-assigned local row
identity, not a server message ID: transcript frames do not carry server message
IDs. It is not a catch-up cursor. Do not assume one message per turn or identify
a message by turn ID alone. Keep IDs stable when updating a local message.

Scope rows and the saved cursor to the appropriate gateway, assistant, device
and conversation so unrelated histories cannot collide. Preserve message and turn
order when reading the history. The physical schema, JSON shape and any additional
metadata required to support existing history behavior are client choices, not a
shared schema or new protocol fields. Store only what current behavior needs.

Persist incremental changes without rewriting every historical conversation.
New Session and server-reported session changes retain the earlier local history
and start a separate conversation. Preserve existing history, sharing and deletion
behavior, including each client's active-session rules. Demo content is not saved.
On iOS, protect the database and its journal files with complete file protection.

## Replay and durability

The reconnect cursor is the last displayed turn ID, not the local message ID.
Apply inclusive replay in server order: replace the anchor turn's displayed
contents with its replayed messages and append subsequent turns, upserting the
local representation rather than duplicating turns or messages. A turn may have
multiple messages, including multiple messages of the same role; preserve each
and its order, not just one row per role. Reconcile replay at the turn level,
reusing local message IDs for corresponding rows where possible and removing
superseded rows. Repeating the same replay must produce the same displayed
history and no duplicates. A running reply snapshot replaces its earlier partial
reply; subsequent live pieces append to that snapshot. Replay never triggers
speech, microphone activation, timers or resubmission.

Persist the affected rows and cursor consistently: never advance the durable
cursor past transcript content that failed to persist. A failed or interrupted
write must permit catch-up instead of silently skipping unsaved content. Delayed
writes must not overwrite newer state or resurrect deleted conversations. For an
unavailable anchor, follow client.md: clear the active view and cursor on
`new_session: true` without assuming the session actually changed; preserve
prior local history. A real session change receives the same signal and starts a
separate history. Existing client presentation and archival rules still apply.

There is no migration or import of prior local history or message-ID cursors,
including existing SQLite and JSON stores. Their local history may be lost at the
switch; do not use an old cursor with the new empty store. Server-side data is
not deleted. Storage errors must not prevent voice startup or an ongoing session;
report failure without transcript content and do not claim a failed save or
deletion succeeded. Preserve recoverable data rather than silently destroying it.

## Acceptance checks for implementation

- Save and restore multiple ordered messages per turn and multiple conversations;
  update a message without rewriting unrelated history.
- Replay an anchor with multiple same-role messages twice, followed by later
  turns; verify order, upserts, no duplicates, and a stable result after relaunch.
- Reconnect during a partial reply, replace it with the snapshot, append live
  pieces, and verify no replayed audio or request is sent.
- Exercise unavailable anchors, real New Session, history and deletion,
  including a delayed-write deletion race; keep other conversations isolated.
- Interrupt or fail a write before cursor persistence and verify catch-up does
  not skip unsaved transcript data; exercise unreadable storage and failed
  deletion without blocking voice or exposing text in diagnostics.
- Verify no old JSON or SQLite history or message-ID cursor is imported, and
  verify iOS database and journal file protection.
