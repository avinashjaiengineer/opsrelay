---
id: RB-006
title: Database failover and connection exhaustion
categories: [dependency]
services: [payments-db]
actions: []
---
Database incidents are not automated: failover, connection-limit changes and restores need the
data on-call. Diagnose, then escalate.

1. Check connection counts, replication lag and error logs.
2. Identify the callers exhausting connections.
3. Escalate to the data team with the evidence; do not restart the database automatically.
