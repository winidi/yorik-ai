---
title: Pipelines — Yorik stays on it
nav_app: pipelines
summary: Follow a sent mail until the answer comes. Yorik looks for the answer in all your mail, also from other addresses, and asks you before every reminder.
---

# Pipelines — Yorik stays on it

You sent a cancellation, a request, a reminder, and now you wait for the answer. A pipeline does the waiting for you: Yorik looks for the answer in your mail every day, and if none comes, he suggests a reminder. Nothing goes out without your okay.

## Starting one

Two ways:

- In **Mail**, open a mail you sent and tap the **Antwort verfolgen** icon in the toolbar.
- In **Pipelines**, tap **Antwort verfolgen** and pick one of your recently sent mails.

Yorik reads your mail and plans the follow-up. He works out what kind of matter it is (a cancellation, a request, a claim), which answer you are waiting for, how many reminders make sense and after how many days, with a short reason for each gap. Then he writes each reminder in the tone of your mail. This takes a few seconds; the page shows **Yorik schreibt die Erinnerungen…** meanwhile. If the language model cannot be reached, a plain template stays and the reminder says so; **Neu schreiben lassen** tries again.

Change the days, the recipients and the texts as you like, then open each reminder once and tap **Freigeben**. **Starten** works once every reminder is approved. If you change a reminder later, its approval is gone and you approve it again.

## How Yorik recognises the answer

Answers often come from another address than the one you wrote to, for example `noreply@service-company.com` for a mail to `cancel@company.de`. So Yorik looks at every mail you received since you sent yours, in all your mail accounts and all folders, spam included. A mail counts as a possible answer when it

- is a reply in the same conversation,
- comes from an address you wrote to, or from the same domain,
- comes from a domain that carries the company's name,
- mentions a number from your mail (customer, contract or invoice number),
- or contains one of the keywords you added.

You can see and change all of this under **Woran Yorik die Antwort erkennt**.

When Yorik finds such a mail, he asks you: **Ist das die Antwort?** Tap **Ja** and the pipeline is done. Tap **Nein, weiter warten** (for example for a mere acknowledgement of receipt) and he keeps waiting. Until you decide, no reminder goes out.

## When a reminder is due

You get a notification: **Keine Antwort — Erinnerung senden?** When a reminder falls due, Yorik writes it again for that day, with what happened since: how long ago you wrote, the reminders already sent, an acknowledgement of receipt that came without a real answer, a concrete deadline in the last reminder. Read the fresh text and tap **Freigeben und senden**. Your approval covers exactly the text you saw. Right before sending, Yorik checks your mail again. If something has arrived in the meantime, he shows it to you instead of sending.

The reminder goes from the same account as your first mail, as a reply in the same conversation, with your first mail quoted below.

## When Yorik cannot check

If a mail account has not synced for a while, reports an error, is still importing, or a mail could not be read, Yorik cannot be sure that nothing came. Then he sends no reminder, says why, and tries again every 15 minutes. You can still tap **Trotzdem senden** if you know the reason does not matter.

## Other things Yorik notices

- **You wrote to the other side yourself.** Yorik stops and asks whether he should keep following.
- **A send was interrupted** (for example by a restart). Yorik never sends twice. He asks you to look in your sent folder and say whether the reminder went out.

## Send times

By default Yorik asks about sending every day between 8 and 20 o'clock. Under **Sendezeiten** you can switch a pipeline to weekdays only (sensible for companies and authorities) or change the hours. Public holidays are not taken into account yet.

## Who can use pipelines

Pipelines are on for everyone. Your pipelines are yours alone. Nobody else in the household sees them, admins included.

Parents and admins can switch pipelines off for a single person at the bottom of the Pipelines app (**Wer darf Pipelines nutzen**). A member cannot switch off an admin. Switching off stops that person's running pipelines until the switch is turned back on. It does not show their content to anyone.

## Coming later

Looking for answers in scanned letters and WhatsApp, an autonomous mode that sends approved reminders by itself, and other kinds of pipelines, such as watching for a payment on your account.
