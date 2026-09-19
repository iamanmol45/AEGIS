from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr as ecr,
    aws_elasticloadbalancingv2 as elbv2,
    aws_cloudwatch as cloudwatch,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
    aws_iam as iam,
    aws_dynamodb as dynamodb,
)
from constructs import Construct


class InfrastructureStack(Stack):

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs
    ) -> None:

        super().__init__(scope, construct_id, **kwargs)

        # -------------------------
        # VPC
        # -------------------------

        self.vpc = ec2.Vpc(
            self,
            "AegisVpc",
            max_azs=2,
            nat_gateways=1,
        )

        # -------------------------
        # ECS Cluster
        # -------------------------

        self.cluster = ecs.Cluster(
            self,
            "AegisCluster",
            vpc=self.vpc,
            cluster_name="aegis-cluster",
        )

        # -------------------------
        # ECR Repository
        # -------------------------

        repository = ecr.Repository.from_repository_name(
            self,
            "AegisApiRepository",
            repository_name="aegis-api",
        )

        # -------------------------
        # ECS Task Definition
        # -------------------------

        task_definition = ecs.FargateTaskDefinition(
            self,
            "AegisApiTaskDefinition",
            cpu=256,
            memory_limit_mib=512,
        )

        task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ecs:DescribeServices",
                    "ecs:UpdateService",
                    "ecs:ListServices",
                ],
                resources=["*"],
            )
        )
        task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:GetMetricData",
                    "cloudwatch:GetMetricStatistics",
                    "cloudwatch:ListMetrics",
                ],
                resources=["*"],
            )
        )
        task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=[
                    "states:StartExecution",
                    "states:DescribeExecution",
                    "states:ListStateMachines",
                    "states:DescribeStateMachine",
                ],
                resources=["*"],
            )
        )

        # -------------------------
        # DynamoDB Incident Store
        # -------------------------

        self.incidents_table = dynamodb.Table(
            self,
            "AegisIncidentsTable",
            table_name="AegisIncidents",
            partition_key=dynamodb.Attribute(
                name="incident_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Grant least privilege to ECS task role
        self.incidents_table.grant(
            task_definition.task_role,
            "dynamodb:PutItem",
            "dynamodb:GetItem",
            "dynamodb:UpdateItem",
            "dynamodb:Scan",
            "dynamodb:Query",
        )

        # -------------------------
        # Container
        # -------------------------

        container = task_definition.add_container(
            "AegisApiContainer",
            image=ecs.ContainerImage.from_ecr_repository(
                repository,
                "latest",
            ),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="aegis-api",
            ),
        )

        container.add_port_mappings(
            ecs.PortMapping(
                container_port=8000,
                protocol=ecs.Protocol.TCP,
            )
        )

        container.add_environment("DYNAMODB_TABLE_NAME", self.incidents_table.table_name)
        container.add_environment("AWS_REGION", self.region)

        # -------------------------
        # ECS Fargate Service
        # -------------------------

        self.service = ecs.FargateService(
            self,
            "AegisApiService",
            cluster=self.cluster,
            task_definition=task_definition,
            desired_count=1,
            assign_public_ip=True,
            circuit_breaker=ecs.DeploymentCircuitBreaker(
                rollback=True
            ),
            min_healthy_percent=100,
        )

        # -------------------------
        # AEGIS Monitoring
        # -------------------------

        cpu_metric = self.service.metric_cpu_utilization(
            period=Duration.minutes(1),
            statistic="Average",
        )

        memory_metric = self.service.metric_memory_utilization(
            period=Duration.minutes(1),
            statistic="Average",
        )

        running_tasks_metric = self.service.metric(
            "RunningTaskCount",
            period=Duration.minutes(1),
            statistic="Minimum",
        )

        # -------------------------
        # CPU Alarm
        # -------------------------

        self.cpu_alarm = cloudwatch.Alarm(
            self,
            "AegisHighCpuAlarm",
            metric=cpu_metric,
            threshold=80,
            evaluation_periods=2,
            datapoints_to_alarm=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        )

        # -------------------------
        # Memory Alarm
        # -------------------------

        self.memory_alarm = cloudwatch.Alarm(
            self,
            "AegisHighMemoryAlarm",
            metric=memory_metric,
            threshold=80,
            evaluation_periods=2,
            datapoints_to_alarm=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        )

        # -------------------------
        # Task Failure Alarm
        # -------------------------

        self.task_count_alarm = cloudwatch.Alarm(
            self,
            "AegisTaskCountAlarm",
            metric=running_tasks_metric,
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        )

        # -------------------------
        # Application Load Balancer
        # -------------------------

        alb = elbv2.ApplicationLoadBalancer(
            self,
            "AegisAlb",
            vpc=self.vpc,
            internet_facing=True,
        )

        # -------------------------
        # ALB Listener
        # -------------------------

        listener = alb.add_listener(
            "AegisListener",
            port=80,
            open=True,
        )

        # -------------------------
        # ECS Target Group
        # -------------------------

        listener.add_targets(
            "AegisApiTarget",
            port=8000,
            targets=[self.service],
            health_check=elbv2.HealthCheck(
                path="/health",
                healthy_http_codes="200",
            ),
        )

        # -------------------------
        # Step Functions Recovery Workflow
        # -------------------------

        scale_out_task = tasks.CallAwsService(
            self,
            "ExecuteScaleOut",
            service="ecs",
            action="updateService",
            parameters={
                "Cluster": self.cluster.cluster_name,
                "Service": self.service.service_name,
                "DesiredCount": sfn.JsonPath.number_at("$.target_desired_count")
            },
            iam_resources=[self.service.service_arn],
            result_path="$.scale_out_result"
        )

        restart_tasks_task = tasks.CallAwsService(
            self,
            "ExecuteRestartTasks",
            service="ecs",
            action="updateService",
            parameters={
                "Cluster": self.cluster.cluster_name,
                "Service": self.service.service_name,
                "DesiredCount": sfn.JsonPath.number_at("$.target_desired_count"),
                "ForceNewDeployment": True
            },
            iam_resources=[self.service.service_arn],
            result_path="$.restart_tasks_result"
        )

        wait_for_ecs = sfn.Wait(
            self,
            "WaitForEcsTaskProvisioning",
            time=sfn.WaitTime.duration(Duration.seconds(30))
        )

        describe_ecs = tasks.CallAwsService(
            self,
            "DescribeEcsService",
            service="ecs",
            action="describeServices",
            parameters={
                "Cluster": self.cluster.cluster_name,
                "Services": [self.service.service_name]
            },
            iam_resources=["*"],
            result_path="$.describe_result"
        )

        recovery_success = sfn.Pass(
            self,
            "RecoverySuccessful",
            parameters={
                "workflow_status": "SUCCESS",
                "recovery_verified": True,
                "incident_id": sfn.JsonPath.string_at("$.incident_id"),
                "action": sfn.JsonPath.string_at("$.action"),
                "desired_count": sfn.JsonPath.number_at("$.describe_result.Services[0].DesiredCount"),
                "running_count": sfn.JsonPath.number_at("$.describe_result.Services[0].RunningCount"),
                "pending_count": sfn.JsonPath.number_at("$.describe_result.Services[0].PendingCount")
            }
        )

        recovery_failed = sfn.Pass(
            self,
            "RecoveryFailedOrEscalated",
            parameters={
                "workflow_status": "FAILED",
                "recovery_verified": False,
                "incident_id": sfn.JsonPath.string_at("$.incident_id"),
                "action": sfn.JsonPath.string_at("$.action"),
                "desired_count": sfn.JsonPath.number_at("$.describe_result.Services[0].DesiredCount"),
                "running_count": sfn.JsonPath.number_at("$.describe_result.Services[0].RunningCount"),
                "reason": "Running task count did not meet target desired count within timeout."
            }
        )

        invalid_action = sfn.Fail(
            self,
            "InvalidRemediationAction",
            error="InvalidActionError",
            cause="The provided remediation action is not supported by AEGIS Step Functions."
        )

        wait_retry = sfn.Wait(
            self,
            "WaitBeforeRetryVerification",
            time=sfn.WaitTime.duration(Duration.seconds(10))
        )

        describe_retry = tasks.CallAwsService(
            self,
            "DescribeEcsServiceRetry",
            service="ecs",
            action="describeServices",
            parameters={
                "Cluster": self.cluster.cluster_name,
                "Services": [self.service.service_name]
            },
            iam_resources=["*"],
            result_path="$.describe_result"
        )

        verify_choice = sfn.Choice(self, "VerifyEcsRecovery")
        verify_retry_choice = sfn.Choice(self, "VerifyEcsRecoveryRetry")
        action_choice = sfn.Choice(self, "ValidateRemediationAction")

        scale_out_task.next(wait_for_ecs)
        restart_tasks_task.next(wait_for_ecs)
        wait_for_ecs.next(describe_ecs)
        describe_ecs.next(verify_choice)

        verify_choice.when(
            sfn.Condition.and_(
                sfn.Condition.number_equals_json_path("$.describe_result.Services[0].RunningCount", "$.target_desired_count"),
                sfn.Condition.number_equals("$.describe_result.Services[0].PendingCount", 0)
            ),
            recovery_success
        ).otherwise(
            wait_retry.next(describe_retry).next(verify_retry_choice)
        )

        verify_retry_choice.when(
            sfn.Condition.and_(
                sfn.Condition.number_equals_json_path("$.describe_result.Services[0].RunningCount", "$.target_desired_count"),
                sfn.Condition.number_equals("$.describe_result.Services[0].PendingCount", 0)
            ),
            recovery_success
        ).otherwise(
            recovery_failed
        )

        action_choice.when(
            sfn.Condition.string_equals("$.action", "SCALE_OUT"),
            scale_out_task
        ).when(
            sfn.Condition.string_equals("$.action", "RESTART_TASKS"),
            restart_tasks_task
        ).otherwise(
            invalid_action
        )

        self.recovery_state_machine = sfn.StateMachine(
            self,
            "AegisRecoveryWorkflow",
            state_machine_name="aegis-recovery-workflow",
            definition_body=sfn.DefinitionBody.from_chainable(action_choice),
            timeout=Duration.minutes(5),
        )

        self.recovery_state_machine.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:UpdateService"],
                resources=[self.service.service_arn]
            )
        )
        self.recovery_state_machine.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeServices"],
                resources=["*"]
            )
        )

        # -------------------------
        # Outputs
        # -------------------------

        CfnOutput(
            self,
            "AegisApiUrl",
            value=f"http://{alb.load_balancer_dns_name}",
        )

        CfnOutput(
            self,
            "AegisRecoveryStateMachineArn",
            value=self.recovery_state_machine.state_machine_arn,
            export_name="AegisRecoveryStateMachineArn"
        )

        CfnOutput(
            self,
            "AegisIncidentsTableName",
            value=self.incidents_table.table_name,
            export_name="AegisIncidentsTableName",
        )