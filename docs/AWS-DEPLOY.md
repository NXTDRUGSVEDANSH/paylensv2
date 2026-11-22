# Deploying PayLens on AWS

The deployment is automated in `.github/workflows/deploy-aws.yml`: GitHub builds
the image, pushes it to ECR, and rolls App Runner onto it. You need to do the
one-time AWS setup below, after which deploying is a button in the Actions tab.

Everything here is run **once**. Total time, first attempt: about 20 minutes.

---

## Why App Runner

The container is immutable and stateless. The database is opened read-only, the
analytical views and the definitional corpus are built into the image, and
nothing written at runtime needs to survive a restart. That removes the usual
reasons to reach for ECS: no volumes, no load balancer, no VPC, no task
definitions. App Runner gives HTTPS and a hostname and nothing else to manage.

Roughly **$25–45/month** at 1 vCPU / 2 GB, part of which is charged on
provisioned memory even when idle.

Do not go below 2 GB. chromadb and onnxruntime are resident before a query
runs, which is why `DUCKDB_MEMORY_LIMIT` is 512MB — on a 2 GB task a larger
budget risks the container being killed under load rather than returning a
slow answer.

---

## Prerequisites

An AWS account, and the AWS CLI on the machine where you run the setup. You do
**not** need Docker locally — GitHub's runners build the image.

```bash
aws configure           # or `aws sso login` if your org uses SSO
aws sts get-caller-identity    # should print your account id
export ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export REGION=ap-south-1
```

---

## 1 · Store the model API key

Never an environment variable in plaintext. App Runner resolves this at
container start.

```bash
aws secretsmanager create-secret \
  --name paylens/llm-api-key \
  --secret-string "sk-or-v1-YOUR-KEY" \
  --region "$REGION"
```

Note the returned `ARN` — it becomes the `LLM_API_KEY_SECRET_ARN` secret in
step 4.

---

## 2 · Let App Runner pull from ECR

App Runner needs its own role to read your private registry.

```bash
aws iam create-role --role-name AppRunnerECRAccess \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow",
      "Principal":{"Service":"build.apprunner.amazonaws.com"},
      "Action":"sts:AssumeRole"}]}'

aws iam attach-role-policy --role-name AppRunnerECRAccess \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess

echo "arn:aws:iam::$ACCOUNT:role/AppRunnerECRAccess"
```

That ARN becomes `AWS_ECR_ACCESS_ROLE_ARN` in step 4.

---

## 3 · Let GitHub deploy, without storing a key

GitHub mints a short-lived OIDC token and AWS trades it for temporary
credentials scoped to one role. There is no long-lived access key to leak,
rotate, or commit by accident — which is the main reason to prefer this over
putting an access key in repository secrets.

```bash
# Register GitHub as an identity provider (once per account)
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1

# Trust ONLY this repository. The `sub` condition is what stops any other
# repository on GitHub assuming your role.
cat > /tmp/trust.json <<JSON
{"Version":"2012-10-17","Statement":[{
  "Effect":"Allow",
  "Principal":{"Federated":"arn:aws:iam::$ACCOUNT:oidc-provider/token.actions.githubusercontent.com"},
  "Action":"sts:AssumeRoleWithWebIdentity",
  "Condition":{
    "StringEquals":{"token.actions.githubusercontent.com:aud":"sts.amazonaws.com"},
    "StringLike":{"token.actions.githubusercontent.com:sub":"repo:NXTDRUGSVEDANSH/paylensv2:*"}}}]}
JSON

aws iam create-role --role-name GitHubDeployPayLens \
  --assume-role-policy-document file:///tmp/trust.json

aws iam attach-role-policy --role-name GitHubDeployPayLens \
  --policy-arn arn:aws:iam::aws:policy/AWSAppRunnerFullAccess
aws iam attach-role-policy --role-name GitHubDeployPayLens \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryPowerUser

# App Runner's service role has to be passable by the deploy role
aws iam put-role-policy --role-name GitHubDeployPayLens \
  --policy-name PassAppRunnerRole \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{
    \"Effect\":\"Allow\",\"Action\":\"iam:PassRole\",
    \"Resource\":\"arn:aws:iam::$ACCOUNT:role/AppRunnerECRAccess\"}]}"

echo "arn:aws:iam::$ACCOUNT:role/GitHubDeployPayLens"
```

That ARN becomes `AWS_DEPLOY_ROLE_ARN` in step 4.

> The two managed policies above are broader than a deployment strictly needs.
> Tighten them to the specific service and repository ARNs once the first
> deploy has worked and you know exactly which calls it makes.

---

## 4 · Add three secrets to GitHub

**Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | the role from step 3 |
| `AWS_ECR_ACCESS_ROLE_ARN` | the role from step 2 |
| `LLM_API_KEY_SECRET_ARN` | the secret ARN from step 1 |

---

## 5 · Deploy

**Actions → deploy to aws → Run workflow.**

It builds the image (several minutes — the dataset is generated and verified
inside the build), pushes it, creates the service on first run and updates it
afterwards, then waits for `RUNNING` and checks the result.

The final step is the one worth keeping. It does not merely confirm the process
is listening — it polls `/health` until `views_built` is `true`. A container
that starts cleanly and then refuses every question because the views are
missing is the failure this deployment has actually had, and a liveness check
would have called it healthy.

The service URL is printed in the workflow summary.

---

## 6 · Lock down CORS

`CORS_ORIGINS` defaults to `*`, which is right for local work and wrong on the
internet. Once you know the hostname, add it to
`RuntimeEnvironmentVariables` in the workflow and re-run:

```yaml
"CORS_ORIGINS": "https://xxxxx.ap-south-1.awsapprunner.com"
```

---

## What runs where

| | |
|---|---|
| Image build | GitHub runner, ~8–12 min (the dataset is generated and verified) |
| Registry | ECR, private |
| Runtime | App Runner, 1 vCPU / 2 GB, one uvicorn worker |
| Model key | Secrets Manager, resolved at container start |
| Database | baked into the image, read-only |
| Demo accounts | SQLite, recreated on each boot — deliberately ephemeral |

One worker per container is deliberate: the query cache is in-process, so a
second worker would keep its own copy and halve the hit rate. Scale by adding
instances, not workers.

## When something is wrong

`GET /health` is the first thing to read. It reports whether the views are
built, whether a model is configured, and which tables are allowed — so it
distinguishes "the deployment is wrong" from "the model is down", which the
logs on their own do not.

| Symptom | Cause |
|---|---|
| `views_built: false` | the image was built before the views step — rebuild |
| `llm_available: false` | the secret ARN is wrong, or the role cannot read it |
| container killed under load | memory; raise the instance before `DUCKDB_MEMORY_LIMIT` |
| first request takes ~25s | the embedding model loading. Expected once per container |
| `CREATE_FAILED` | almost always `AWS_ECR_ACCESS_ROLE_ARN` — App Runner could not pull |
