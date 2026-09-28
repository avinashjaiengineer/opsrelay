---
id: RB-007
title: Investigating an unexplained error rate
categories: [unknown]
services: []
actions: []
---
When no cause is clear:

1. Check every dependency's health; a failing dependency makes callers look broken.
2. Compare the error start time with deployments, config changes and traffic.
3. Sample the logs for the top error signatures.
4. If nothing explains it, escalate with what you checked. Do not guess a remediation.
