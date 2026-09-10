---
title: Recordings — dinners and meetings, with what came out of them
nav_app: recordings
summary: Record a dinner or a meeting on the tablet or phone; Yorik writes the transcript with speakers and a report with tasks, nice moments and friction. Only the people at the table see it. Everything stays on your Yorik box.
---

# Recordings — dinners and meetings, with what came out of them

Yorik can listen to a whole conversation at the table and write it down afterwards: who said what, what was decided, who took on which task, the nice moments worth keeping, and what did not go well. Nothing leaves your Yorik box; the transcript and the report are visible only to the people you named when you started.

## Starting a recording

Three ways, same result:

- **On the kiosk tablet:** tap **Record dinner** on the wall. Sign in as yourself (avatar + PIN), tick who is at the table, tap **Start recording**. The photo wall stays on; a small red pill shows the running time.
- **On your phone or computer:** open the **Recordings** app (the microphone in the dock), tap **Record**, tick who is here, start.
- **By voice or in chat:** "Yorik, record the dinner with Beate and Tom." Yorik answers that the recording runs.

Tell the table that you are recording. The person who starts it is responsible for that; the red pill is visible the whole time.

The recording uploads a piece every two minutes, so a crash or an empty battery costs minutes, not the dinner. If the tablet's screen locks or someone else signs in on it, the recording carries on.

## Ending it

Tap **Stop** on the pill, or say **"Yorik, the dinner is over."** Yorik joins the pieces and starts working. This takes a few minutes on a normal PC and up to half an hour on an old laptop; everybody you ticked gets a notification on their phone when the report is ready.

## Speakers

Yorik separates the voices and tries to put names on them using the voice profiles people enrolled in **Settings → Voice**. Someone without a profile appears as "Speaker 2" (or "Sprecher 2" if you use Yorik in German). Enrolling takes ten seconds of talking and makes the reports much more useful, because tasks then land on the right name.

Kids without a profile stay anonymous in the transcript.

## The report

For dinners and meetings Yorik writes the report right after the transcript:

- **Tasks** — things one person will do, with the name Yorik heard and a date when one was mentioned. They are proposals; everybody at the table sees the whole list. Tap **Adopt** on the ones that are yours: the task becomes yours and shows up in your day plan the next morning ("Plan my day"). For something you do together (the registry office, the doctor with your child) tick the others in the small box before adding; then all of you get it. What nobody adopted stays visibly open.
- **Decisions** — agreements that are not one person's to-do.
- **Nice moments** — praise, successes, plans people were happy about.
- **What did not go well** — annoyances and things left unresolved, so they can be talked about calmly later.
- **Open questions** and **dates mentioned**.

For a plain "conversation" recording there is no automatic report; open it and tap **Write the report** when you want one. Ask in chat too: "What came out of yesterday's dinner?"

## Privacy and storage

- Only the people ticked at the start can open the recording, its transcript and its report. An admin who was not at the table cannot.
- The audio is kept for 30 days and then deleted; the transcript and the report stay. Change the period with `HOMEOS_RECORDING_RETENTION_DAYS` in `config.env`, or delete a recording yourself with the bin icon.
- The speech and speaker models run on the CPU of your Yorik box. The first recording downloads them (about 35 MB).

## Good to know

- Microphone quality decides everything. A tablet two metres away with plates clattering will mix up speakers now and then; the transcript itself is usually fine.
- Safari on iPhone stops recording when the screen locks; on Android and on the kiosk tablet the screen stays on while recording.
- A recording that is never stopped fails after a day and can be deleted.
