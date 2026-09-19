import os
import aws_cdk as cdk

from infrastructure.infrastructure_stack import InfrastructureStack


app = cdk.App()

account = os.getenv("CDK_DEFAULT_ACCOUNT") or os.getenv("AWS_ACCOUNT_ID")
region = os.getenv("CDK_DEFAULT_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-south-1"

InfrastructureStack(
    app,
    "AegisInfrastructureStack",
    env=cdk.Environment(account=account, region=region) if account else None,
)

app.synth()