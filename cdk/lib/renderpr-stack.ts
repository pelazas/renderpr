import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as ssm from "aws-cdk-lib/aws-ssm";
import * as apigwv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as apigwv2_integrations from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { PythonFunction } from "@aws-cdk/aws-lambda-python-alpha";
import { Construct } from "constructs";
import * as path from "path";

export class RenderprStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const appName = "renderpr";

    // VPC: public subnets only, 1 AZ, no NAT Gateway
    const vpc = new ec2.Vpc(this, "Vpc", {
      maxAzs: 1,
      natGateways: 0,
      subnetConfiguration: [
        {
          name: "Public",
          subnetType: ec2.SubnetType.PUBLIC,
        },
      ],
    });

    // Tags for cost allocation
    cdk.Tags.of(this).add("Project", appName);

    // Security group: outbound-only for Fargate (no inbound)
    const fargateSg = new ec2.SecurityGroup(this, "FargateSecurityGroup", {
      vpc,
      allowAllOutbound: true,
      description: "Allows outbound traffic only; no inbound. For RenderPR Fargate tasks.",
    });

    // SSM parameter names (created manually via scripts/setup-secrets.sh)
    const githubParamName = `/${appName}/github-app`;
    const openrouterParamName = `/${appName}/openrouter`;

    // SSM parameter ARNs for IAM
    const githubParamArn = cdk.Arn.format(
      { service: "ssm", resource: "parameter", resourceName: `${appName}/github-app` },
      this,
    );
    const openrouterParamArn = cdk.Arn.format(
      { service: "ssm", resource: "parameter", resourceName: `${appName}/openrouter` },
      this,
    );

    // IAM: Lambda execution role
    const lambdaRole = new iam.Role(this, "LambdaRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole",
        ),
      ],
    });

    // IAM: Fargate execution role (pull image, write logs)
    const fargateExecutionRole = new iam.Role(this, "FargateExecutionRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AmazonECSTaskExecutionRolePolicy",
        ),
      ],
    });

    // IAM: Fargate task role (what container code uses at runtime)
    const fargateTaskRole = new iam.Role(this, "FargateTaskRole", {
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
    });

    fargateTaskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter"],
        resources: [githubParamArn, openrouterParamArn],
      }),
    );

    // Lambda function (Python 3.12)
    const handler = new PythonFunction(this, "WebhookHandler", {
      entry: path.join(__dirname, "../../src/lambda"),
      index: "webhook_handler.py",
      handler: "handler",
      runtime: lambda.Runtime.PYTHON_3_12,
      role: lambdaRole,
      timeout: cdk.Duration.seconds(10),
      memorySize: 128,
    });

    // API Gateway HTTP API
    const httpApi = new apigwv2.HttpApi(this, "WebhookApi", {
      apiName: `${appName}-webhook`,
      description: "RenderPR GitHub webhook receiver",
    });

    httpApi.addRoutes({
      path: "/",
      methods: [apigwv2.HttpMethod.POST],
      integration: new apigwv2_integrations.HttpLambdaIntegration(
        "WebhookIntegration",
        handler,
      ),
    });

    // ECS Cluster
    const cluster = new ecs.Cluster(this, "FargateCluster", {
      vpc,
      clusterName: `${appName}-cluster`,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    // Fargate task definition
    const taskDef = new ecs.FargateTaskDefinition(this, "ReviewTaskDef", {
      family: `${appName}-review`,
      cpu: 512,
      memoryLimitMiB: 1024,
      executionRole: fargateExecutionRole,
      taskRole: fargateTaskRole,
    });

    taskDef.addContainer("ReviewContainer", {
      image: ecs.ContainerImage.fromAsset(path.join(__dirname, "../..")),
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: appName }),
      environment: {
        GITHUB_PARAM_NAME: githubParamName,
        OPENROUTER_PARAM_NAME: openrouterParamName,
        IDLE_TIMEOUT: this.node.tryGetContext("idleTimeoutSeconds") ?? "900",
        POLL_INTERVAL: this.node.tryGetContext("pollIntervalSeconds") ?? "10",
      },
    });

    // Pass infrastructure references to Lambda
    handler.addEnvironment("ECS_CLUSTER_ARN", cluster.clusterArn);
    handler.addEnvironment(
      "ECS_TASK_DEFINITION_ARN",
      taskDef.taskDefinitionArn,
    );
    handler.addEnvironment(
      "SUBNET_IDS",
      vpc.publicSubnets.map((s) => s.subnetId).join(","),
    );
    handler.addEnvironment("SECURITY_GROUP_ID", fargateSg.securityGroupId);
    handler.addEnvironment("GITHUB_PARAM_NAME", githubParamName);

    // Allow Lambda to pass the Fargate roles to ECS (required by RunTask)
    fargateExecutionRole.grantPassRole(lambdaRole);
    fargateTaskRole.grantPassRole(lambdaRole);

    // Grant Lambda permission to run and describe tasks
    lambdaRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ecs:RunTask", "ecs:DescribeTasks"],
        resources: [
          taskDef.taskDefinitionArn,
          cluster.clusterArn,
          cdk.Arn.format(
            {
              service: "ecs",
              resource: "task",
              resourceName: `${cluster.clusterName}/*`,
            },
            this,
          ),
        ],
      }),
    );

    // Outputs
    new cdk.CfnOutput(this, "ApiGatewayUrl", {
      value: httpApi.url ?? "",
      description: "GitHub App webhook URL",
    });

    new cdk.CfnOutput(this, "ClusterArn", {
      value: cluster.clusterArn,
      description: "ECS Cluster ARN",
    });

    new cdk.CfnOutput(this, "GitHubParamName", {
      value: githubParamName,
      description: "SSM Parameter Store name for GitHub App credentials",
    });

    new cdk.CfnOutput(this, "TaskDefinitionArn", {
      value: taskDef.taskDefinitionArn,
      description: "Fargate task definition ARN",
    });
  }
}
