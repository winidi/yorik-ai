---
name: recording_status
description: Show the state of a recording and, once it is done, the transcript with speakers.
when_to_use: |
  Use it when the user asks whether the recording is running or finished, or wants to know what was said ("zeig mir das Transkript", "was haben wir beim Abendessen besprochen").
  Without `recording_id` it returns the user's latest recording; the transcript is in `transcript`, one line per turn with time and speaker.
  Answer questions about the content from `transcript`; quote speakers by name and do not invent turns that are not there.
when_not_to_use: |
  The user wants the structured report with tasks; that is recording_report.
inputs:
  recording_id:
    type: integer
    required: false
    description: The recording to look at; defaults to the latest one the user may see.
outputs:
  status:
    type: string
    description: recording, uploaded, processing, done or failed.
  progress:
    type: string
    description: Pipeline step while processing.
  transcript:
    type: string
    description: "[mm:ss] Name: text" per turn, present when done (long ones are cut at 12000 characters).
  speakers:
    type: array
cost: instant
permissions: [admin, member, restricted]
side_effects: none
tags: [recordings, voice, read]
category: system
---

# recording_status

Read-only. Visibility follows the recording's participants: the person
who started it and the people named at start. A restricted account sees
a recording only when it was listed as a participant.
