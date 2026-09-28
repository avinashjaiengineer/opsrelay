---
id: RB-004
title: checkout-api recovery
categories: [bad-deploy, memory-leak, dependency]
services: [checkout-api]
actions: [rollback_deployment, restart_service]
---
checkout-api is tier 1: failed checkouts lose revenue immediately.

1. Check payments-db and inventory-service first: checkout-api fails when either does (RB-006).
2. If checkout-api alone is failing after a release, follow RB-001 and roll back.
3. The promotion engine (PriceCalculator) is the most frequent source of release regressions; look
   for its exceptions in the logs.
4. Never flush the pricing cache during peak hours without the payments on-call.
