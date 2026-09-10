---
name: start_recording
description: Start recording a conversation at the table so Yorik can write the transcript with speakers afterwards.
when_to_use: |
  Use it when the user asks to record the dinner, a meeting or a conversation ("nimm das Abendessen auf", "record this").
  Pass the names of the people at the table as `participants`; they will be able to see the transcript, nobody else.
  The device the user is talking to captures the audio after this call; tell the user the recording has started and that "das Abendessen ist fertig" ends it.
  If the result has `unknown`, ask which household members those names meant.
when_not_to_use: |
  A single voice question to Yorik (that is the normal voice flow, not a recording). Anything the user did not explicitly ask to record.
inputs:
  title:
    type: string
    required: false
    description: Short name for the recording, e.g. "Abendessen 10.9." (optional, defaults by kind and date).
  kind:
    type: string
    required: false
    description: dinner, meeting or conversation (default conversation).
  participants:
    type: array
    required: false
    description: Names of the household members present, as the user said them.
outputs:
  recording_id:
    type: integer
  participants:
    type: array
    description: Resolved members (user_id, name).
  unknown:
    type: array
    description: Names that matched no household member.
cost: instant
permissions: [admin, member]
side_effects: Creates a recording row; the device starts capturing audio and uploads it to Yorik.
tags: [recordings, voice, write]
category: system
---

# start_recording

Creates the recording and tells the UI to start capturing (ui_action
`start_recording`). Audio is uploaded in chunks to
`/api/recordings/{id}/chunk`; `finish_recording` or the UI's Stop
button closes it and starts the transcript pipeline (see
`backend/recordings.py`).

Consent is the caller's: the person who starts the recording is
responsible for telling the table. The device shows a recording
indicator the whole time.
