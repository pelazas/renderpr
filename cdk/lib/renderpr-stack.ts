import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as ssm from "aws-cdk-lib/aws-ssm";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as apigwv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as apigwv2_integrations from "aws-cdk-lib/aws-apigatewayv2-integrations";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import { PythonFunction } from "@aws-cdk/aws-lambda-python-alpha";
import { Construct } from "constructs";
import * as path from "path";
import * as fs from "fs";

export class RenderprStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const appName = "renderpr";

    // Cost / abuse-control knobs (tunable via `cdk deploy -c <key>=<value>`).
    const maxConcurrentTasks = String(
      this.node.tryGetContext("maxConcurrentTasks") ?? "5",
    );
    const maxTaskAgeSeconds = String(
      this.node.tryGetContext("maxTaskAgeSeconds") ?? "1500",
    );
    const reaperIntervalMinutes = Number(
      this.node.tryGetContext("reaperIntervalMinutes") ?? 5,
    );
    const webhookRateLimit = Number(
      this.node.tryGetContext("webhookRateLimit") ?? 20,
    );
    const webhookBurstLimit = Number(
      this.node.tryGetContext("webhookBurstLimit") ?? 40,
    );
    const tasksParamPrefix = `/${appName}/tasks`;

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
      allowAllOutbound: false,
      description: "Egress restricted to web/DNS ports and dev server preview on port 3000. For RenderPR Fargate tasks.",
    });

    // Live-preview dev-server ports, one per framework family. Keep in sync with
    // FRAMEWORK_DEFAULT_PORTS in src/agent/config.py: 3000 (next/cra/remix/spa),
    // 5173 (vite/sveltekit), 4321 (astro). Without the right port open, the
    // public preview link is unreachable even though the dev server is running.
    for (const previewPort of [3000, 4321, 5173]) {
      fargateSg.addIngressRule(
        ec2.Peer.anyIpv4(),
        ec2.Port.tcp(previewPort),
        `Allow live preview access to dev server on :${previewPort}`,
      );
    }

    fargateSg.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(3001),
      "Allow Lambda to dispatch commands to command server",
    );

    // Egress is restricted to standard web + DNS ports (threat-model F-5): this
    // blocks C2 / exfiltration over arbitrary ports and non-web protocols while
    // still allowing package installs, git/GitHub, OpenRouter, AWS APIs, the ECS
    // metadata/credentials endpoint (169.254.170.2:80), and DNS. NOTE: it does NOT
    // stop exfiltration over HTTPS to an arbitrary public host — that needs a
    // domain-allowlist egress proxy / NAT+firewall, which would require leaving the
    // public-subnet model and is tracked as a follow-up.
    fargateSg.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), "HTTPS: registries, GitHub, OpenRouter, AWS APIs");
    fargateSg.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), "HTTP: package CDNs + ECS metadata/credentials endpoint");
    fargateSg.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.udp(53), "DNS (UDP)");
    fargateSg.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(53), "DNS (TCP)");

    // S3 bucket for screenshot hosting
    const screenshotBucket = new s3.Bucket(this, "ScreenshotBucket", {
      bucketName: `${appName}-screenshots-${this.account}`,
      publicReadAccess: false,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      autoDeleteObjects: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      lifecycleRules: [
        {
          id: "ScreenshotRetention",
          enabled: true,
          prefix: "screenshots/",
          expiration: cdk.Duration.days(7),
        },
        {
          id: "NpmCacheExpiration",
          enabled: true,
          prefix: "npm-cache/",
          expiration: cdk.Duration.days(7),
        },
      ],
    });

    // Screenshots of private/authenticated UI must not be world-readable
    // (threat-model F-6): the bucket is private and screenshots are served only
    // through CloudFront with Origin Access Control + signed URLs. The signing
    // public key is read from CDK context or a gitignored PEM that
    // scripts/setup-cloudfront-key.sh writes (the matching private key lives in
    // SSM). Fail loudly if neither is present so we never deploy a broken signer.
    const cfPublicKeyPath = path.join(__dirname, "../cloudfront-public-key.pem");
    const cfPublicKeyPem: string | undefined =
      this.node.tryGetContext("cloudfrontPublicKey") ??
      (fs.existsSync(cfPublicKeyPath) ? fs.readFileSync(cfPublicKeyPath, "utf8") : undefined);
    if (!cfPublicKeyPem) {
      throw new Error(
        "CloudFront signing public key not found. Run scripts/setup-cloudfront-key.sh " +
        "(writes cdk/cloudfront-public-key.pem and stores the private key in SSM) before deploying, " +
        "or pass -c cloudfrontPublicKey=\"$(cat cloudfront-public-key.pem)\".",
      );
    }
    const cfPublicKey = new cloudfront.PublicKey(this, "ScreenshotSigningKey", { encodedKey: cfPublicKeyPem });
    const cfKeyGroup = new cloudfront.KeyGroup(this, "ScreenshotKeyGroup", { items: [cfPublicKey] });
    const screenshotDistribution = new cloudfront.Distribution(this, "ScreenshotDistribution", {
      comment: "RenderPR screenshots — private bucket via OAC, signed URLs only",
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(screenshotBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        trustedKeyGroups: [cfKeyGroup],
      },
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
    const commandTokenParamArn = cdk.Arn.format(
      { service: "ssm", resource: "parameter", resourceName: `${appName}/renderpr-command-token` },
      this,
    );
    // CloudFront URL-signing private key (created by scripts/setup-cloudfront-key.sh).
    const cloudfrontKeyParamName = `/${appName}/cloudfront-private-key`;
    const cloudfrontKeyParamArn = cdk.Arn.format(
      { service: "ssm", resource: "parameter", resourceName: `${appName}/cloudfront-private-key` },
      this,
    );
    const tasksParamArn = cdk.Arn.format(
      { service: "ssm", resource: "parameter", resourceName: `${appName}/tasks/*` },
      this,
    );
    // Path-root ARN (no trailing /*) — required to authorize GetParametersByPath
    // over the /renderpr/tasks hierarchy alongside the child wildcard above.
    const tasksParamPathArn = cdk.Arn.format(
      { service: "ssm", resource: "parameter", resourceName: `${appName}/tasks` },
      this,
    );
    // Per-repo user secrets (env vars, auth signing secrets, provider keys),
    // one parameter per secret under /{appName}/secrets/{installation}/{repo}/{KEY}.
    const secretsParamArn = cdk.Arn.format(
      { service: "ssm", resource: "parameter", resourceName: `${appName}/secrets/*` },
      this,
    );
    // Path-root ARN (no trailing /*) — required to authorize GetParametersByPath
    // over the /renderpr/secrets hierarchy alongside the child wildcard above.
    const secretsParamPathArn = cdk.Arn.format(
      { service: "ssm", resource: "parameter", resourceName: `${appName}/secrets` },
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

    lambdaRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParameter", "ssm:DeleteParameter"],
        resources: [githubParamArn, commandTokenParamArn, tasksParamArn],
      }),
    );

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
        resources: [githubParamArn, openrouterParamArn, commandTokenParamArn, cloudfrontKeyParamArn],
      }),
    );

    fargateTaskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ssm:PutParameter", "ssm:DeleteParameter"],
        resources: [tasksParamArn],
      }),
    );

    // Read per-repo user secrets for env/auth injection (fork PRs are gated in code).
    // The task role does NOT hold the broad secrets read directly (threat-model F-2):
    // instead it assumes SecretsAccessRole per task with an inline session policy
    // scoped to the current repo's path, so a task can only ever read its own repo's
    // secrets — never another installation's or repo's.
    const secretsAccessRoleName = `${appName}-secrets-access`;
    const secretsAccessRoleArn = cdk.Arn.format(
      { service: "iam", resource: "role", resourceName: secretsAccessRoleName, region: "" },
      this,
    );
    const secretsAccessRole = new iam.Role(this, "SecretsAccessRole", {
      roleName: secretsAccessRoleName,
      assumedBy: new iam.ArnPrincipal(fargateTaskRole.roleArn),
      description:
        "Assumed per-task by the review container with an inline session policy " +
        "scoped to one repo's secrets path, so the task can only read the current " +
        "repo's secrets (threat-model F-2).",
      maxSessionDuration: cdk.Duration.hours(1),
    });
    secretsAccessRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParametersByPath", "ssm:GetParameter", "ssm:GetParameters"],
        resources: [secretsParamArn, secretsParamPathArn],
      }),
    );

    // Reference the role by a constructed ARN string (not secretsAccessRole.roleArn)
    // to avoid a CloudFormation circular dependency between the two roles.
    fargateTaskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["sts:AssumeRole"],
        resources: [secretsAccessRoleArn],
      }),
    );

    fargateTaskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:PutObject"],
        resources: [screenshotBucket.arnForObjects("*")],
      }),
    );

    fargateTaskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
        resources: [screenshotBucket.arnForObjects("npm-cache/*")],
      }),
    );

    fargateTaskRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ec2:DescribeNetworkInterfaces"],
        resources: ["*"],
      }),
    );

    // Lambda function (Python 3.12)
    const handler = new PythonFunction(this, "WebhookHandler", {
      entry: path.join(__dirname, "../../src/lambda_handler"),
      index: "webhook_handler.py",
      handler: "handler",
      runtime: lambda.Runtime.PYTHON_3_12,
      role: lambdaRole,
      timeout: cdk.Duration.seconds(30),
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

    // Edge rate limiting: cap requests hitting the Lambda so the public webhook
    // can't be flooded into spinning Fargate tasks. The HMAC check already drops
    // unsigned spam, but throttling bounds invocation volume regardless.
    const defaultStage = httpApi.defaultStage?.node
      .defaultChild as apigwv2.CfnStage;
    defaultStage.defaultRouteSettings = {
      throttlingRateLimit: webhookRateLimit,
      throttlingBurstLimit: webhookBurstLimit,
    };

    // ECS Cluster
    const cluster = new ecs.Cluster(this, "FargateCluster", {
      vpc,
      clusterName: `${appName}-cluster`,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    // Fargate task definition
    const taskDef = new ecs.FargateTaskDefinition(this, "ReviewTaskDef", {
      family: `${appName}-review`,
      // Full-page screenshots under SwiftShader (software GL, no GPU on Fargate)
      // are memory-hungry; 1 GB shared with Node + the dev server segfaulted the
      // Chromium renderer (signal 11). 2 GB / 1 vCPU gives headroom.
      cpu: 1024,
      memoryLimitMiB: 2048,
      executionRole: fargateExecutionRole,
      taskRole: fargateTaskRole,
    });

    taskDef.addContainer("ReviewContainer", {
      image: ecs.ContainerImage.fromAsset(path.join(__dirname, "../..")),
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: appName }),
      environment: {
        GITHUB_PARAM_NAME: githubParamName,
        OPENROUTER_PARAM_NAME: openrouterParamName,
        COMMAND_TOKEN_PARAM_NAME: "/renderpr/renderpr-command-token",
        SECRETS_ACCESS_ROLE_ARN: secretsAccessRoleArn,
        SCREENSHOT_BUCKET: screenshotBucket.bucketName,
        CLOUDFRONT_DOMAIN: screenshotDistribution.distributionDomainName,
        CLOUDFRONT_KEY_PAIR_ID: cfPublicKey.publicKeyId,
        CLOUDFRONT_PRIVATE_KEY_PARAM: cloudfrontKeyParamName,
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
    handler.addEnvironment("COMMAND_TOKEN_PARAM_NAME", "/renderpr/renderpr-command-token");
    handler.addEnvironment("MAX_CONCURRENT_TASKS", maxConcurrentTasks);
    handler.addEnvironment("TASKS_PARAM_PREFIX", tasksParamPrefix);

    // Allow Lambda to pass the Fargate roles to ECS (required by RunTask)
    fargateExecutionRole.grantPassRole(lambdaRole);
    fargateTaskRole.grantPassRole(lambdaRole);

    // Grant Lambda permission to run, describe, tag, and stop tasks
    // (StopTask backs per-PR replace of a task reviewing a superseded commit).
    lambdaRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ecs:RunTask", "ecs:DescribeTasks", "ecs:TagResource", "ecs:StopTask"],
        resources: [
          `arn:aws:ecs:${this.region}:${this.account}:task-definition/${taskDef.family}*`,
          cluster.clusterArn,
          cdk.Arn.format(
            {
              service: "ecs",
              resource: "task",
              resourceName: `${cluster.clusterName}/*`,
            },
            this,
          ),
          cdk.Arn.format(
            {
              service: "ecs",
              resource: "container-instance",
              resourceName: `${cluster.clusterName}/*`,
            },
            this,
          ),
        ],
      }),
    );

    // ecs:ListTasks does not support resource-level scoping — it must be granted
    // on "*" (the ecs:cluster condition key does not apply to it either), so the
    // concurrency-cap count needs this separate broad statement.
    lambdaRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ecs:ListTasks"],
        resources: ["*"],
      }),
    );

    lambdaRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ec2:DescribeNetworkInterfaces"],
        resources: ["*"],
      }),
    );

    // Reusable ARN for any task in this cluster.
    const clusterTaskArn = cdk.Arn.format(
      { service: "ecs", resource: "task", resourceName: `${cluster.clusterName}/*` },
      this,
    );

    // --- Orphan-task reaper -------------------------------------------------
    // Scheduled Lambda that force-stops review tasks older than the max lifetime
    // and sweeps stale /renderpr/tasks/* registrations — the backstop for
    // containers that never self-terminate.
    const reaperRole = new iam.Role(this, "ReaperRole", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AWSLambdaBasicExecutionRole",
        ),
      ],
    });

    // ecs:ListTasks requires Resource "*" (no resource-level scoping); the
    // task-scoped actions stay constrained to this cluster's tasks.
    reaperRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ecs:ListTasks"],
        resources: ["*"],
      }),
    );
    reaperRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ecs:DescribeTasks", "ecs:StopTask"],
        resources: [cluster.clusterArn, clusterTaskArn],
      }),
    );
    // GetParametersByPath authorizes against the path hierarchy, so grant both
    // the path root and its descendants; DeleteParameter targets the children.
    reaperRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ssm:GetParametersByPath", "ssm:DeleteParameter"],
        resources: [tasksParamPathArn, tasksParamArn],
      }),
    );

    const reaper = new PythonFunction(this, "ReaperHandler", {
      entry: path.join(__dirname, "../../src/lambda_handler"),
      index: "reaper_handler.py",
      handler: "handler",
      runtime: lambda.Runtime.PYTHON_3_12,
      role: reaperRole,
      timeout: cdk.Duration.seconds(60),
      memorySize: 128,
      environment: {
        ECS_CLUSTER_ARN: cluster.clusterArn,
        MAX_TASK_AGE_SECONDS: maxTaskAgeSeconds,
        TASKS_PARAM_PREFIX: tasksParamPrefix,
        // Scope destructive reaping to this stack's review tasks only.
        TASK_FAMILY: taskDef.family,
      },
    });

    new events.Rule(this, "ReaperSchedule", {
      schedule: events.Schedule.rate(cdk.Duration.minutes(reaperIntervalMinutes)),
      targets: [new targets.LambdaFunction(reaper)],
      description: "Periodically reap stale RenderPR Fargate tasks",
    });

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

    new cdk.CfnOutput(this, "ScreenshotBucketName", {
      value: screenshotBucket.bucketName,
      description: "S3 bucket for PR review screenshots",
    });

    new cdk.CfnOutput(this, "ScreenshotDistributionDomain", {
      value: screenshotDistribution.distributionDomainName,
      description: "CloudFront domain serving signed screenshot URLs",
    });
  }
}
