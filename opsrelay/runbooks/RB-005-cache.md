---
id: RB-005
title: Cache errors and timeouts
categories: [dependency]
services: [redis-cache]
actions: [flush_cache, restart_service]
---
Symptoms: callers log timeouts to redis-cache, cache hit rate drops, latency rises across services
that share the cache.

1. Confirm the timeouts come from the cache, not the callers (compare several callers).
2. If the cache holds corrupt or oversized entries, flush_cache.
3. If the cache process is unhealthy, restart_service on redis-cache.
4. Verify callers' latency recovers.
