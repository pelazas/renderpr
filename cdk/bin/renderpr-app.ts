#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { RenderprStack } from "../lib/renderpr-stack";

const app = new cdk.App();
new RenderprStack(app, "RenderprStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});
