---
id: RB-003
title: CPU saturation from a traffic spike
categories: [saturation]
services: []
actions: [scale_service]
---
Symptoms: CPU above 85% across replicas, request queues growing, p99 latency rising, request rate
well above baseline (a campaign, a flash sale, a retry storm).

1. Confirm CPU is above 85% on every replica and the request rate is above baseline.
2. Rule out a bad deploy (no recent release) and a retry storm from a dependency.
3. Remediate: scale_service to about 2x the current replicas, within max_replicas.
4. Verify CPU falls below 70% and p99 latency returns to baseline.

Rollback plan: scale back to the previous replica count once traffic subsides.
