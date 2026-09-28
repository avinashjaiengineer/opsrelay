---
id: RB-002
title: Memory exhaustion and OOMKilled pods
categories: [memory-leak]
services: []
actions: [restart_service, rollback_deployment]
---
Symptoms: memory near the container limit, pods OOMKilled and restarting, latency rising as
garbage collection thrashes.

1. Confirm memory is above 90% of the limit and pods are being OOMKilled.
2. Check whether a recent deploy changed memory behaviour; if so, prefer rollback_deployment.
3. Otherwise remediate: restart_service to reclaim memory. This buys time; it is not a fix.
4. Do NOT scale out: more replicas of a leaking service leak more memory.
5. Verify memory and latency recover; file a follow-up to find the leak (heap dump, cache sizes).
