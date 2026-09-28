"""Connectors to real infrastructure, behind the `Environment` interface (opsrelay.environment).

- catalog.py:    the service catalog (YAML)
- cloudwatch.py: metrics and logs from Amazon CloudWatch
- ecs.py:        deployments and actions on Amazon ECS, reconcilable after a crash
- aws.py:        AwsEnvironment, which combines them (OPSRELAY_ENVIRONMENT=aws)

To add another system (EKS, your deploy tool, a CMDB), implement the same small interfaces and
compose them in an Environment.
"""
