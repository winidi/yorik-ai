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
  deleted_bill_id:
    type: integer
  bill:
    type: object
tags: [bills, mutation, destructive]
permissions: [admin, member, restricted]
---
# delete_bill
Hard 1-row cap. Cancel/test re-inserts.
