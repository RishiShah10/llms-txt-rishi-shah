# Deployment Guide

Terraform for the backend: an ECS Fargate service (ARM64) behind an ALB with an ACM
cert on a Route53 domain, a public-read S3 bucket for the generated files, and an
EventBridge schedule for the nightly auto-update sweep.

## 📋 Table of Contents

- [Prerequisites](#prerequisites)
- [AWS Account Setup](#aws-account-setup)
- [Neon Database Setup](#neon-database-setup)
- [Optional Services (Bright Data, OpenRouter)](#optional-services)
- [Terraform Configuration](#terraform-configuration)
- [Backend Deployment (Docker + ECS)](#backend-deployment-docker--ecs)
- [Frontend Deployment](#frontend-deployment)
- [DNS and SSL](#dns-and-ssl)
- [Verification](#verification)
- [Monitoring and Logs](#monitoring-and-logs)
- [Updates and Maintenance](#updates-and-maintenance)
- [Troubleshooting](#troubleshooting)
- [Cleanup](#cleanup)

## Prerequisites

### Required Tools

```bash
aws --version        # AWS CLI v2+
terraform --version  # v1.5+
docker --version     # any recent version
```

```bash
brew install awscli terraform docker
```

### Required Accounts

- **AWS account** with admin access.
- **Neon** (serverless Postgres) — free tier works. Any Postgres reachable over the
  internet works; this project was built on Neon.
- **A domain hosted in Route53** — the backend runs on a subdomain (e.g.
  `api.example.com`); Terraform provisions the cert against it.
- **Bright Data** *(optional)* — a Scraping Browser CDP endpoint, for the "Unblock
  protected sites" feature.
- **OpenRouter** *(optional)* — an API key, for "AI-enhanced descriptions".

Only the database is required. The two optional keys are blank by default.

## AWS Account Setup

### 1. Configure the AWS CLI

```bash
aws configure
```

Provide your Access Key ID, Secret Access Key, a default region (e.g. `us-east-1`),
and `json` output.

> **Using AWS SSO / `aws login` instead?** Terraform and Docker cannot read a
> token-based session natively. Bridge it into environment variables before every
> terraform/docker command:
> ```bash
> eval "$(aws configure export-credentials --format env)"
> ```

### 2. Verify access

```bash
aws sts get-caller-identity
```

### 3. Get VPC and subnet IDs

> **⚠️ MUST EXIST — Terraform does NOT create these.** The ALB and Fargate tasks
> attach to a VPC and at least two subnets in different AZs that you provide. Most
> AWS accounts have a default VPC you can use as-is.

```bash
# Default VPC
aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
  --query "Vpcs[0].VpcId" --output text

# Subnets in that VPC (pick 2+ in different AZs)
aws ec2 describe-subnets \
  --filters "Name=vpc-id,Values=vpc-xxxxxxxx" \
  --query "Subnets[*].[SubnetId,AvailabilityZone]" --output table
```

**No default VPC** (some accounts have had it deleted)? Recreate one — this makes a
VPC with public subnets, an internet gateway, and routing:

```bash
aws ec2 create-default-vpc --query "Vpc.VpcId" --output text
# then re-run the describe-subnets command above to get its subnet IDs
```

### 4. Get the Route53 hosted zone ID

> **⚠️ MUST EXIST — Terraform does NOT create this.** The backend runs on a
> subdomain (`api_domain`, e.g. `api.example.com`) and Terraform provisions the
> TLS cert and the alias record *inside a Route53 hosted zone you already own*. The
> domain must be registered and its DNS hosted in Route53.

Already have the domain in Route53:

```bash
aws route53 list-hosted-zones \
  --query "HostedZones[].{name:Name,id:Id}" --output table
```

Own the domain elsewhere (e.g. Namecheap, GoDaddy)? Create a hosted zone, then
point your registrar's nameservers at the four `NS` records it returns:

```bash
aws route53 create-hosted-zone \
  --name example.com \
  --caller-reference "$(date +%s)" \
  --query "{zoneId:HostedZone.Id, nameservers:DelegationSet.NameServers}"
```

Don't own a domain yet? Register one through the Route53 console
(Route53 → Registered domains → Register domains) — the hosted zone is created for
you automatically.

Use the zone ID (the part after `/hostedzone/`) as `route53_zone_id`.

## Neon Database Setup

1. Create a project at [neon.tech](https://neon.tech).
2. Copy the **pooled** connection string. It looks like:
   ```
   postgresql://user:pass@ep-xxx-pooler.REGION.aws.neon.tech/neondb?sslmode=require
   ```
3. **No migration step is needed** — the backend runs `db.init_db()` on startup,
   which creates the `generations` table and its columns idempotently. A wrong
   `database_url` won't crash the app or fail `/health`; persistence just degrades
   silently, so double-check it.

## Optional Services

Both are left blank in `terraform.tfvars` by default; skip either.

- **Bright Data** (browser rendering for JS-heavy / bot-walled sites): create a
  *Scraping Browser* zone and copy its CDP URL —
  `wss://brd-customer-...:...@brd.superproxy.io:9222`. Set `bright_data_cdp_url`.
- **OpenRouter** (LLM descriptions): create an API key at
  [openrouter.ai](https://openrouter.ai). Set `openrouter_api_key`. The default
  model is `openai/gpt-4o-mini`.

## Terraform Configuration

### 1. Copy the example variables

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

### 2. Rename the S3 bucket (required)

S3 bucket names are **globally unique**, and this repo hardcodes one. Open
`s3.tf` and change the bucket name to something unique to you:

```hcl
resource "aws_s3_bucket" "files" {
  bucket = "YOUR-UNIQUE-llms-files-bucket"   # <-- change this
}
```

Nothing else references the literal name — `ecs.tf` reads it from
`aws_s3_bucket.files.bucket`, so it flows through automatically.

### 3. Fill in `terraform.tfvars`

```hcl
aws_region      = "us-east-1"
environment     = "production"
api_domain      = "api.example.com"                 # the subdomain the backend runs on
route53_zone_id = "Z0123456789ABCDEFGHIJ"           # hosted zone id for that domain
vpc_id          = "vpc-xxxxxxxx"                     # from AWS setup
subnet_ids      = ["subnet-aaaaaaaa", "subnet-bbbbbbbb"]
image_tag       = "latest"                           # "latest" for first bootstrap; git SHA thereafter
alarm_email     = "you@example.com"                  # CloudWatch alarms are emailed here

database_url        = "postgresql://user:pass@host/db?sslmode=require"
openrouter_api_key  = ""                             # blank disables AI descriptions
openrouter_model    = "openai/gpt-4o-mini"
openrouter_base_url = "https://openrouter.ai/api/v1"
bright_data_cdp_url = ""                             # blank disables browser rendering
frontend_origin     = "https://your-frontend.vercel.app"   # allowed CORS + WebSocket Origin
cron_secret         = ""                             # generate below
```

> `terraform.tfvars` holds real secrets and is gitignored. Do **not** commit it.

There is **no API key / WebSocket auth token** to configure — the WebSocket is
guarded by an `Origin` check against `frontend_origin`.

### 4. Generate the cron secret

```bash
openssl rand -base64 32
```

Paste it into `cron_secret`. EventBridge sends it as the `X-Cron-Secret` header to
the refresh endpoint; the endpoint is fail-shut (rejects everything if it's unset).

### 5. Initialize Terraform

```bash
terraform init
```

## Backend Deployment (Docker + ECS)

Terraform creates the ECR repository, so the image can't be pushed until the repo
exists — hence the two-step bootstrap below.

> **The `aws` CLI commands below hardcode `--region us-east-1` to match the example
> `aws_region`.** If you set a different region in `terraform.tfvars`, substitute it
> in every `--region` flag, or the commands will target the wrong region and report
> "service not found" / login failures.

### 1. Create the ECR repository first

```bash
terraform apply -target=aws_ecr_repository.api
```

### 2. Build (ARM64) and push the image

The task runs on **Graviton (ARM64)**. Apple Silicon builds arm64 by default; on an
x86 machine or in CI you **must** pass `--platform linux/arm64`, or the task dies
with `exec format error`.

```bash
REGISTRY=$(terraform output -raw ecr_repository_url | cut -d/ -f1)
ECR=$(terraform output -raw ecr_repository_url)

aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin "$REGISTRY"

docker build --platform linux/arm64 -t llms-api ../backend
docker tag llms-api:latest "$ECR:latest"
docker push "$ECR:latest"
```

### 3. Apply the full infrastructure

```bash
terraform apply
```

Review the plan, type `yes`. Creates the ECS cluster/service, ALB, ACM certificate
(validated via Route53), S3 bucket + policy, IAM roles, EventBridge schedule, and
CloudWatch alarms. **~5–10 min**, mostly ACM validation and the ALB going healthy.

### 4. Verify the service is healthy

```bash
aws ecs describe-services --cluster llms-cluster --services llms-api-service \
  --region us-east-1 \
  --query "services[0].{running:runningCount,desired:desiredCount,rollout:deployments[0].rolloutState}"
```

Healthy = `running == desired` (2) and `rollout == COMPLETED`.

```bash
curl "https://$(terraform output -raw api_url | sed 's|https://||')/health"
# {"status":"ok"}
```

## Frontend Deployment

The frontend is a Next.js app deployed to Vercel (see `frontend/README.md`).

```bash
cd ../frontend
vercel --prod
```

Set one environment variable in the Vercel dashboard:

- `NEXT_PUBLIC_API_URL` = `https://api.example.com` (your `api_domain`, **https**).

The client derives the WebSocket URL (`wss://…`) from it. If it's left unset or set
to `http://`, the browser blocks the socket as mixed content. Then add that Vercel
domain to `frontend_origin` in `terraform.tfvars` and re-apply, so the WebSocket
`Origin` check accepts it.

## DNS and SSL

**Automatic, as long as `api_domain`'s hosted zone is in Route53** (`route53_zone_id`).
On `terraform apply`, `acm.tf`:

1. Requests a DNS-validated ACM certificate for `api_domain`.
2. Writes the validation record into the Route53 zone and waits for it to validate.
3. Creates the alias `A` record pointing `api_domain` at the ALB.

No manual CNAME entry, no separate validation step. If your domain is **not** in
Route53, you'd instead create the ACM validation record and the alias at your DNS
provider by hand.

```bash
terraform output acm_certificate_arn   # the issued cert
terraform output alb_dns_name          # what the alias points at
```

## Verification

```bash
API=$(terraform output -raw api_url)

# 1. health
curl "$API/health"                                  # {"status":"ok"}

# 2. a real generation over the WebSocket (needs the `websockets` python lib)
python3 - <<'PY'
import asyncio, json, websockets
async def main():
    # replace api.example.com with your api_domain
    async with websockets.connect("wss://api.example.com/ws/generate") as ws:
        await ws.send(json.dumps({"url": "https://llmstxt.org", "max_pages": 5, "crawl": True}))
        while True:
            f = json.loads(await ws.recv())
            if f["type"] != "event":
                print(f["type"], f.get("result", {}).get("public_url", f.get("detail")))
                break
asyncio.run(main())
PY
```

## Monitoring and Logs

```bash
# Live application logs
aws logs tail /ecs/llms-backend --region us-east-1 --follow

# The nightly sweep logs its progress
aws logs tail /ecs/llms-backend --region us-east-1 --since 1h | grep "refresh sweep"
```

CloudWatch alarms (5xx rate, unhealthy hosts, CPU, memory) publish to an SNS topic; the
address in `alarm_email` gets a subscription-confirmation email on first apply —
confirm it or the alarms can't notify you.

## Updates and Maintenance

To ship a backend code change:

```bash
SHA=$(git rev-parse --short HEAD)
ECR=$(terraform -chdir=terraform output -raw ecr_repository_url)

docker build --platform linux/arm64 -t llms-api backend
docker tag llms-api:latest "$ECR:$SHA"
docker push "$ECR:$SHA"

# set image_tag = "<SHA>" in terraform.tfvars, THEN:
terraform -chdir=terraform apply
```

Set `image_tag` in `terraform.tfvars`, not just `-var` — a later plain `apply`
reverts to the file's tag and rolls the image back. Config/secret-only changes
(no code) skip the rebuild: edit `terraform.tfvars` and `terraform apply`.

Deploys use a circuit breaker with auto-rollback, and autoscaling (min 2 / max 6 on
CPU) owns the task count via `ignore_changes = [desired_count]`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `exec format error` in the task logs | Image built for x86. Rebuild with `--platform linux/arm64`. |
| Terraform: "No valid credential sources found" | Token-based AWS session. Run `eval "$(aws configure export-credentials --format env)"` first. |
| Service stuck at 0 running tasks | The `image_tag` isn't in ECR yet — push it, then apply. |
| ALB returns 503 | Tasks still starting (wait ~2 min) or crash-looping (`aws logs tail /ecs/llms-backend`). |
| `terraform apply` hangs on `aws_acm_certificate_validation` | `route53_zone_id` is wrong, or the domain isn't actually in that zone. |
| `BucketAlreadyExists` on apply | The S3 bucket name is global — pick a different one in `s3.tf`. |
| Plain `terraform apply` rolled the image back | `image_tag` in `terraform.tfvars` is stale; set it to the current SHA. |

## Cleanup

```bash
# S3 must be emptied before Terraform can delete the bucket
aws s3 rm "s3://$(terraform output -raw files_bucket)" --recursive

terraform destroy
```

This tears down every resource created here. The Neon database, the Vercel project,
and the Route53 hosted zone are not managed by this Terraform and are left intact.

## Everything You Must Have Before `terraform apply`

Terraform creates the ECR repo, ECS cluster/service, ALB, ACM certificate, S3
bucket, IAM roles, EventBridge schedule, CloudWatch alarms, and the Route53
validation + alias **records**. It does **not** create the following — these must
exist (or be filled in) first:

| # | Must exist | How | Goes into |
|---|---|---|---|
| 1 | **AWS account** with admin, CLI configured | `aws configure` → `aws sts get-caller-identity` | — |
| 2 | **VPC** | usually the default; else `aws ec2 create-default-vpc` | `vpc_id` |
| 3 | **2+ subnets** in different AZs | `aws ec2 describe-subnets …` | `subnet_ids` |
| 4 | **Route53 hosted zone** for your domain | `aws route53 list-hosted-zones` / `create-hosted-zone` | `route53_zone_id` |
| 5 | A **subdomain** to run on (needn't pre-exist as a record) | you choose, e.g. `api.example.com` | `api_domain` |
| 6 | **Neon** (or any) Postgres connection string | create a project at neon.tech | `database_url` |
| 7 | **A unique S3 bucket name** | edit `s3.tf` (names are global) | `s3.tf` |
| 8 | **cron secret** | `openssl rand -base64 32` | `cron_secret` |
| 9 | **Frontend origin** (after deploying to Vercel) | the Vercel URL | `frontend_origin` |
| 10 | **Bright Data CDP URL** — *optional* | Scraping Browser zone | `bright_data_cdp_url` |
| 11 | **OpenRouter API key** — *optional* | openrouter.ai | `openrouter_api_key` |

Rows 1–8 are required for a working deploy. Row 9 is set after the frontend is up.
Rows 10–11 are optional features that degrade gracefully when left blank.
