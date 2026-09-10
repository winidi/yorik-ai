---
name: finish_recording
description: End the running recording so Yorik writes the transcript with speakers.
when_to_use: |
  Use it when the user says the dinner, meeting or conversation is over ("das Abendessen ist fertig", "stop recording", "alle Themen sind besprochen").
  Without `recording_id` it ends the user's currently running recording.
  Tell the user the transcript takes a few minutes and that everybody at the table gets a notification when it is ready.
when_not_to_use: |
  Nothing is being recorded; then say so instead of calling it.
inputs:
  recording_id:
    type: integer
    required: false
    description: The recording to end; defaults to the user's running one.
outputs:
  recording_id:
    type: integer
  status:
    type: string
    description: uploaded or processing when the device has handed over its audio, else recording with stop_requested true.
cost: instant; the transcript itself takes minutes in the background
permissions: [admin, member]
side_effects: Stops the capture on the device, joins the audio and starts the speaker + transcript pipeline.
tags: [recordings, voice, write]
category: system
---

# finish_recording

Two cases. When the audio is already on the server (the UI uploaded its
last chunk, or a script streamed the file), the recording is finished
here and the pipeline starts. When a device is still capturing, the
skill only sets `stop_requested`; the device polls its recording, uploads
the tail and calls `/finish` itself, because only it knows when the last
bytes are out. Either way the caller sees a status they can read back.
