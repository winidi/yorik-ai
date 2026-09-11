---
name: remember_planning_rule
description: Remember one sentence about how the person wants their days planned, for every future day plan.
when_to_use: |
  Use it after the user confirmed that a correction should stick ("Küche macht Beate", "vormittags keine Anrufe", "höchstens sechs Punkte am Tag").
  Ask once before calling it; never store a rule the user did not confirm.
  Pass the rule as one plain sentence in the user's words.
when_not_to_use: |
  One-off changes to today's plan; those go into the draft, not into the rules.
inputs:
  rule:
    type: string
    required: true
    description: One sentence, e.g. "Küche und Einkauf macht Beate."
outputs:
  rules:
    type: string
    description: All rules now stored, one per line.
cost: instant
permissions: [admin, member]
side_effects: Appends a line to the person's planning rules (Settings > You > Planning rules).
tags: [planning, write]
category: planning
---

# remember_planning_rule

The rules are free text on the profile (`user_profiles.planning_rules`)
and go into every plan_my_day run as `rules`. Editing or deleting them
happens in Settings; this skill only appends.
