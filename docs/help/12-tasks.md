---
title: Tasks — recurring, briefing integration
nav_app: tasks
summary: Add tasks via natural language, mark done, recurring tasks, overdue tracking, briefing integration.
---

# Tasks — recurring, briefing integration

Yorik's task list is the "to-do" surface. Lives in the same DB as everything else; the briefing pulls today's + overdue.

## Adding tasks

- **Chat**: *"Müll rausbringen am Sonntag"* / *"Erinner mich an Tabletten heute"*.
- **Voice**: same, spoken.
- **Tasks app**: top text input, type, Enter. Yorik picks category + priority via the LLM.

The chat path uses `add_task`. Date math is deterministic — *"morgen"*, *"nächsten Donnerstag"*, *"in 3 Tagen"* all resolve to ISO dates without LLM arithmetic.

## Recurring tasks

In chat: *"jeden Sonntag Müll rausbringen"* — Yorik sets `recurrence_rule="weekly"` + the next instance. When you mark it done, Yorik auto-materialises the next occurrence.

Supported shorthand: `daily`, `weekly`, `biweekly`, `monthly`, `quarterly`, `yearly`, `every N days|weeks|months|years`, `every Mon`, `every Mon,Wed,Fri`.

## Marking done

- **Chat**: *"hak Tabletten ab"* / *"Müll ist erledigt"*.
- **UI**: checkbox next to the task.

Done tasks disappear from the active list. Done tab shows the last 7 days.

## Overdue tracking

Tasks past their due date show in red at the top of the list AND appear in the morning briefing under "überfällig". The briefing prompts you about them daily until done or rescheduled.

## Briefing integration

The morning briefing (Briefing app, or *"was steht heute an"* in chat) combines:

- Today's events
- Today's + overdue tasks
- Bills due in the next 7 days
- Photos from yesterday (recent activity)
- Email digest (new + actionable since last briefing)

Each task in the briefing is clickable → opens the Tasks app with that task highlighted.

## AI-assisted estimates + categories

When you add a task, Yorik can auto-fill:

- **Category** (Errands / Health / Work / Family / ...) based on the description.
- **Estimated minutes** based on similar past tasks.

Both are suggestions — overridable in the task editor.

## Planning a day

Say "plan meinen Tag" (or "plan my day"). Yorik collects the day's fixed
appointments, your open tasks, what yesterday left over and yesterday's
review, asks the household's agent for the daily briefing if one is
connected, and drafts the day: a short list with two to four focus blocks
and the rest as plain tasks. Change it as often as you like ("move the
writing block to 15:00", "drop the call"). When you say it is good, Yorik
writes it: one task per item, time blocks in your private **Plan**
calendar (they never appear in the shared calendar), one card that undoes
the whole day.

In the evening, "wie war mein Tag" gives a short review: what got done,
where the task timer's actual minutes differed from the estimate, what
carries over. Tomorrow's planning starts from that.

