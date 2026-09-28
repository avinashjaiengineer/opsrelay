#!/usr/bin/env python3
import os

import aws_cdk as cdk
from stack import OpsRelayStack

app = cdk.App()
OpsRelayStack(
    app,
    app.node.try_get_context("stack_name") or "OpsRelay",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
)
app.synth()
