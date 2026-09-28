---
id: RB-001
title: Error spike after a deployment
categories: [bad-deploy]
services: []
actions: [rollback_deployment]
---
Symptoms: the 5xx error rate or exceptions rise sharply right after a release; latency often rises too.

1. Compare the error-rate increase with the latest deployment's timestamp. A rise within minutes
   of a release points at the release.
2. Check the logs for exception types that are new in the release (NullPointerException,
   ClassNotFound, serialization errors).
3. Rule out dependencies: if downstream services are also failing, see RB-005 or RB-006 instead.
4. Remediate: rollback_deployment to the previous version.
5. Verify the error rate is back under 1% within 5 minutes, then open a bug for the release owner.

Rollback plan: redeploy the rolled-back version once it is fixed.
