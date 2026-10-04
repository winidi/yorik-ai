---
name: delete_bill
description: Delete ONE bill by id
when_to_use: |
  - User says "lösch die Stromrechnung" / "delete that bill"
inputs:
  bill_id:
    type: integer
    required: true
    description: Exactly ONE bill id per call.
outputs:
  pending:
    type: boolean
    description: Always true — the delete is staged, not executed. It runs only when the user taps Delete on the card.
  pending_id:
    type: string
  bill:
    type: object
    description: The row that WOULD be deleted.
tags: [bills, mutation, destructive]
permissions: [admin, member]
---
# delete_bill
Hard 1-row cap. Confirm-before-apply — nothing is deleted until the user taps Delete on the card; Keep discards it. Reply that the card is waiting, never that the bill is gone.
