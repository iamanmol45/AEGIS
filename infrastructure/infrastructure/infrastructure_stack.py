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
    aws_cloudwatch_actions as cw_actions,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
    aws_iam as iam,
    aws_dynamodb as dynamodb,
    aws_rds as rds,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    aws_events as events,
    aws_events_targets as event_targets,
    aws_budgets as budgets,
    aws_s3 as s3,
    aws_cloudtrail as cloudtrail,
    aws_sqs as sqs,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_event_sources,
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
            nat_gateways=2,
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

        # ecs:ListServices has no resource-level permission support in IAM,
        # so it stays wildcard. DescribeServices/UpdateService are scoped to
        # the specific service ARNs below, once those services exist.
        task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=["ecs:ListServices"],
                resources=["*"],
            )
        )
        # CloudWatch metric read APIs do not support resource-level IAM
        # permissions (AWS requires "*" for GetMetricData/ListMetrics).
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
        # states:ListStateMachines has no resource-level permission support.
        # StartExecution/DescribeExecution are granted via
        # recovery_state_machine.grant_* once the state machine is defined.
        task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=["states:ListStateMachines"],
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
        # S3 Evidence Bucket
        # -------------------------
        # Holds raw incident evidence (correlated signals, metric
        # detections, RCA payloads) -- DynamoDB only stores a reference key
        # to keep incident items small. 30-day expiry keeps storage cost
        # bounded for a short-lived hackathon deployment.

        self.evidence_bucket = s3.Bucket(
            self,
            "AegisEvidenceBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireEvidenceAfter30Days",
                    expiration=Duration.days(30),
                )
            ],
        )

        self.evidence_bucket.grant_read_write(task_definition.task_role)

        # -------------------------
        # CloudTrail (AWS API Audit)
        # -------------------------
        # Separate from the evidence bucket: CloudTrail manages its own
        # bucket policy for cloudtrail.amazonaws.com writes, and mixing
        # that with app-managed evidence objects/lifecycle rules is more
        # fragile than just letting the construct own a dedicated bucket.

        self.trail = cloudtrail.Trail(
            self,
            "AegisTrail",
            trail_name="aegis-trail",
            is_multi_region_trail=True,
            management_events=cloudtrail.ReadWriteType.ALL,
            send_to_cloud_watch_logs=False,
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
        container.add_environment("EVIDENCE_BUCKET_NAME", self.evidence_bucket.bucket_name)
        container.add_environment("AWS_REGION", self.region)

        # -------------------------
        # ECS Fargate Service
        # -------------------------

        api_service_name = "aegis-api-service"

        self.service = ecs.FargateService(
            self,
            "AegisApiService",
            service_name=api_service_name,
            cluster=self.cluster,
            task_definition=task_definition,
            desired_count=1,
            assign_public_ip=True,
            circuit_breaker=ecs.DeploymentCircuitBreaker(
                rollback=True
            ),
            min_healthy_percent=100,
        )

        # Scope ECS recovery permissions to this specific service ARN only.
        # Built from literal strings (not self.service.service_arn) because
        # the API task role's own policy cannot depend on the API service
        # construct without creating a circular CloudFormation dependency
        # (service -> task role policy -> service). Giving the service an
        # explicit, known name lets us form the ARN without that token.
        api_service_arn = self.format_arn(
            service="ecs",
            resource="service",
            resource_name=f"{self.cluster.cluster_name}/{api_service_name}",
        )
        task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeServices", "ecs:UpdateService"],
                resources=[api_service_arn],
            )
        )

        container.add_environment("ECS_SERVICE", api_service_name)

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

        # ECS publishes "LiveTaskCount" for a service under AWS/ECS, not
        # "RunningTaskCount" -- the latter doesn't exist, so an alarm on it
        # never receives data and can never fire (caught via the
        # verification runbook: this alarm sat in INSUFFICIENT_DATA since
        # its original creation, pre-dating this session's changes).
        running_tasks_metric = self.service.metric(
            "LiveTaskCount",
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
        # RDS Database (PostgreSQL) - Phase 2
        # -------------------------

        self.db_security_group = ec2.SecurityGroup(
            self,
            "AegisDbSecurityGroup",
            vpc=self.vpc,
            description="Security group for AEGIS RDS PostgreSQL database",
            allow_all_outbound=True,
        )

        self.db_security_group.add_ingress_rule(
            peer=self.service.connections.security_groups[0],
            connection=ec2.Port.tcp(5432),
            description="Allow PostgreSQL access from AegisApiService",
        )

        self.database = rds.DatabaseInstance(
            self,
            "AegisDatabase",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_16
            ),
            # T3 (x86) instead of T4G (Graviton) -- the target AZs hit
            # ServiceLimitExceeded/capacity errors on T4G + gp2 twice in a
            # row; T3 has broader AZ availability for small dev instances.
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE3,
                ec2.InstanceSize.MICRO,
            ),
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[self.db_security_group],
            database_name="aegis",
            allocated_storage=20,
            max_allocated_storage=20,
            publicly_accessible=False,
            storage_encrypted=True,
            multi_az=False,
            removal_policy=RemovalPolicy.DESTROY,
            delete_automated_backups=True,
        )

        # -------------------------
        # ECS Fargate Worker Service - Phase 2
        # -------------------------

        worker_task_definition = ecs.FargateTaskDefinition(
            self,
            "AegisWorkerTaskDefinition",
            cpu=256,
            memory_limit_mib=512,
        )

        worker_task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=["ecs:ListServices"],
                resources=["*"],
            )
        )
        worker_task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:GetMetricData",
                    "cloudwatch:GetMetricStatistics",
                    "cloudwatch:ListMetrics",
                    "cloudwatch:PutMetricData",
                ],
                resources=["*"],
            )
        )
        worker_task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=["states:ListStateMachines"],
                resources=["*"],
            )
        )

        self.incidents_table.grant(
            worker_task_definition.task_role,
            "dynamodb:PutItem",
            "dynamodb:GetItem",
            "dynamodb:UpdateItem",
            "dynamodb:Scan",
            "dynamodb:Query",
        )

        self.evidence_bucket.grant_read_write(worker_task_definition.task_role)

        if self.database.secret:
            self.database.secret.grant_read(task_definition.task_role)
            self.database.secret.grant_read(worker_task_definition.task_role)

        worker_container = worker_task_definition.add_container(
            "AegisWorkerContainer",
            image=ecs.ContainerImage.from_ecr_repository(
                repository,
                "latest",
            ),
            command=["python", "supervisor.py"],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="aegis-worker",
            ),
        )

        worker_container.add_environment("DYNAMODB_TABLE_NAME", self.incidents_table.table_name)
        worker_container.add_environment("EVIDENCE_BUCKET_NAME", self.evidence_bucket.bucket_name)
        worker_container.add_environment("AWS_REGION", self.region)
        worker_container.add_environment("ECS_CLUSTER", self.cluster.cluster_name)
        worker_container.add_environment("AEGIS_POLL_INTERVAL", "60")
        worker_container.add_environment("DB_HOST", self.database.db_instance_endpoint_address)
        worker_container.add_environment("DB_PORT", self.database.db_instance_endpoint_port)

        container.add_environment("DB_HOST", self.database.db_instance_endpoint_address)
        container.add_environment("DB_PORT", self.database.db_instance_endpoint_port)

        self.worker_service = ecs.FargateService(
            self,
            "AegisWorkerService",
            cluster=self.cluster,
            task_definition=worker_task_definition,
            desired_count=1,
            assign_public_ip=True,
            circuit_breaker=ecs.DeploymentCircuitBreaker(
                rollback=True
            ),
            min_healthy_percent=100,
        )

        self.db_security_group.add_ingress_rule(
            peer=self.worker_service.connections.security_groups[0],
            connection=ec2.Port.tcp(5432),
            description="Allow PostgreSQL access from AegisWorkerService",
        )

        # The worker performs recovery actions against the API service, so
        # it needs the same scoped ECS permission on that service's ARN.
        worker_task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeServices", "ecs:UpdateService"],
                resources=[api_service_arn],
            )
        )

        worker_container.add_environment("ECS_SERVICE", api_service_name)

        # -------------------------
        # Step Functions Recovery Workflow
        # -------------------------

        # Cluster/Service/iam_resources below use the literal
        # api_service_name/api_service_arn strings, not self.service.*.
        # The state machine's task role also gets StartExecution granted to
        # the API task role (further down), so a token reference back to
        # self.service here would recreate the service <-> task-role cycle
        # fixed above -- literals give the same ARN without the dependency.
        scale_out_task = tasks.CallAwsService(
            self,
            "ExecuteScaleOut",
            service="ecs",
            action="updateService",
            parameters={
                "Cluster": self.cluster.cluster_name,
                "Service": api_service_name,
                "DesiredCount": sfn.JsonPath.number_at("$.target_desired_count")
            },
            iam_resources=[api_service_arn],
            result_path="$.scale_out_result"
        )

        restart_tasks_task = tasks.CallAwsService(
            self,
            "ExecuteRestartTasks",
            service="ecs",
            action="updateService",
            parameters={
                "Cluster": self.cluster.cluster_name,
                "Service": api_service_name,
                "DesiredCount": sfn.JsonPath.number_at("$.target_desired_count"),
                "ForceNewDeployment": True
            },
            iam_resources=[api_service_arn],
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
                "Services": [api_service_name]
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
                "Services": [api_service_name]
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

        # Uses the literal api_service_arn (not self.service.service_arn):
        # the API task role also grants itself StartExecution on this state
        # machine below, so a token reference back to the service here
        # would reintroduce the service <-> task-role circular dependency
        # fixed above.
        self.recovery_state_machine.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:UpdateService"],
                resources=[api_service_arn]
            )
        )
        self.recovery_state_machine.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeServices"],
                resources=["*"]
            )
        )

        # Scope StartExecution/DescribeExecution to this specific state
        # machine instead of the earlier account-wide "states:*" wildcard.
        self.recovery_state_machine.grant_start_execution(task_definition.task_role)
        self.recovery_state_machine.grant_read(task_definition.task_role)
        self.recovery_state_machine.grant_start_execution(worker_task_definition.task_role)
        self.recovery_state_machine.grant_read(worker_task_definition.task_role)

        # -------------------------
        # SNS Alerts + Escalation
        # -------------------------

        self.alerts_topic = sns.Topic(
            self,
            "AegisAlertsTopic",
            topic_name="aegis-alerts",
            display_name="AEGIS Alerts",
        )

        # Notify whenever a recovery workflow execution fails, times out,
        # or is aborted -- this is the escalation path for BLOCK / failed
        # recovery cases described in the architecture doc.
        events.Rule(
            self,
            "AegisRecoveryFailureRule",
            event_pattern=events.EventPattern(
                source=["aws.states"],
                detail_type=["Step Functions Execution Status Change"],
                detail={
                    "status": ["FAILED", "TIMED_OUT", "ABORTED"],
                    "stateMachineArn": [self.recovery_state_machine.state_machine_arn],
                },
            ),
            targets=[event_targets.SnsTopic(self.alerts_topic)],
        )

        sns_action = cw_actions.SnsAction(self.alerts_topic)
        self.cpu_alarm.add_alarm_action(sns_action)
        self.memory_alarm.add_alarm_action(sns_action)
        self.task_count_alarm.add_alarm_action(sns_action)

        # -------------------------
        # Worker Heartbeat Watchdog
        # -------------------------
        # supervisor.py emits a custom "HeartbeatCount" metric every poll
        # cycle. If no heartbeat arrives for two 5-minute periods, the
        # detection loop has stalled (crashed thread, hung cycle, task
        # replacement) -- treat missing data as a breach so silence itself
        # triggers the alarm, not just an explicit bad value.
        heartbeat_metric = cloudwatch.Metric(
            namespace="AEGIS/Supervisor",
            metric_name="HeartbeatCount",
            period=Duration.minutes(5),
            statistic="Sum",
        )

        self.heartbeat_alarm = cloudwatch.Alarm(
            self,
            "AegisSupervisorHeartbeatAlarm",
            metric=heartbeat_metric,
            threshold=1,
            evaluation_periods=2,
            datapoints_to_alarm=2,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )
        self.heartbeat_alarm.add_alarm_action(sns_action)

        # -------------------------
        # Detection Trigger: EventBridge -> SQS -> Lambda
        # -------------------------
        # Decouples anomaly detection from supervisor.py's fixed poll
        # interval: when a metric alarm fires, this path kicks an
        # immediate cycle instead of waiting up to AEGIS_POLL_INTERVAL
        # seconds. supervisor.py keeps polling independently as a
        # fallback/backstop in case an alarm-driven trigger is dropped.
        #
        # The Lambda itself stays a thin trigger (stdlib only, no VPC) --
        # it POSTs to the already-running API's /cycle endpoint rather
        # than reimplementing detection, since that would mean bundling
        # scikit-learn/numpy/pydantic into a Lambda package. The API
        # container already has those dependencies loaded.

        self.detection_dlq = sqs.Queue(
            self,
            "AegisDetectionDLQ",
            queue_name="aegis-detection-dlq",
            retention_period=Duration.days(14),
        )

        self.detection_queue = sqs.Queue(
            self,
            "AegisDetectionQueue",
            queue_name="aegis-detection-queue",
            visibility_timeout=Duration.seconds(60),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=self.detection_dlq,
            ),
        )

        events.Rule(
            self,
            "AegisDetectionTriggerRule",
            event_pattern=events.EventPattern(
                source=["aws.cloudwatch"],
                detail_type=["CloudWatch Alarm State Change"],
                resources=[
                    self.cpu_alarm.alarm_arn,
                    self.memory_alarm.alarm_arn,
                    self.task_count_alarm.alarm_arn,
                ],
                detail={"state": {"value": ["ALARM"]}},
            ),
            targets=[event_targets.SqsQueue(self.detection_queue)],
        )

        detection_trigger_fn = lambda_.Function(
            self,
            "AegisDetectionTriggerFunction",
            function_name="aegis-detection-trigger",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            timeout=Duration.seconds(29),
            environment={
                "AEGIS_API_URL": f"http://{alb.load_balancer_dns_name}",
            },
            code=lambda_.Code.from_inline(
                "import json\n"
                "import os\n"
                "import urllib.request\n"
                "\n"
                "def handler(event, context):\n"
                "    api_url = os.environ['AEGIS_API_URL']\n"
                "    req = urllib.request.Request(f'{api_url}/cycle', method='POST', data=b'')\n"
                "    with urllib.request.urlopen(req, timeout=25) as resp:\n"
                "        body = resp.read().decode()\n"
                "    print(f'AEGIS cycle triggered: {body}')\n"
                "    return {'statusCode': 200, 'body': body}\n"
            ),
        )

        detection_trigger_fn.add_event_source(
            lambda_event_sources.SqsEventSource(self.detection_queue, batch_size=1)
        )

        # If the trigger keeps failing (bad deploy, API down) messages land
        # in the DLQ after 3 attempts -- alert on that instead of failing
        # silently.
        dlq_alarm = cloudwatch.Alarm(
            self,
            "AegisDetectionDLQAlarm",
            metric=self.detection_dlq.metric_approximate_number_of_messages_visible(),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        )
        dlq_alarm.add_alarm_action(sns_action)

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

        CfnOutput(
            self,
            "AegisRdsEndpoint",
            value=self.database.db_instance_endpoint_address,
            export_name="AegisRdsEndpoint",
        )

        CfnOutput(
            self,
            "AegisRdsPort",
            value=self.database.db_instance_endpoint_port,
            export_name="AegisRdsPort",
        )

        CfnOutput(
            self,
            "AegisWorkerServiceName",
            value=self.worker_service.service_name,
            export_name="AegisWorkerServiceName",
        )

        CfnOutput(
            self,
            "AegisAlertsTopicArn",
            value=self.alerts_topic.topic_arn,
            export_name="AegisAlertsTopicArn",
        )

        CfnOutput(
            self,
            "AegisEvidenceBucketName",
            value=self.evidence_bucket.bucket_name,
            export_name="AegisEvidenceBucketName",
        )

        CfnOutput(
            self,
            "AegisTrailArn",
            value=self.trail.trail_arn,
            export_name="AegisTrailArn",
        )

        CfnOutput(
            self,
            "AegisDetectionQueueUrl",
            value=self.detection_queue.queue_url,
            export_name="AegisDetectionQueueUrl",
        )

        # -------------------------
        # Cost Guardrail
        # -------------------------
        # AWS Budgets is a global service, tracks the whole account's spend
        # (not just this stack), and needs no infra to run -- a free
        # tripwire against the account's $120 credit. Two thresholds so an
        # early warning arrives well before the hard cap.
        budgets.CfnBudget(
            self,
            "AegisMonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="aegis-monthly-cost-guardrail",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=100,
                    unit="USD",
                ),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="ACTUAL",
                        comparison_operator="GREATER_THAN",
                        threshold=50,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="EMAIL",
                            address="bhanusreey@gmail.com",
                        )
                    ],
                ),
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="ACTUAL",
                        comparison_operator="GREATER_THAN",
                        threshold=80,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=[
                        budgets.CfnBudget.SubscriberProperty(
                            subscription_type="EMAIL",
                            address="bhanusreey@gmail.com",
                        )
                    ],
                ),
            ],
        )