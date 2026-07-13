# Deployment

Backend → AWS ECS Fargate (Terraform), region **us-east-2** (co-located with Neon).
Frontend → Vercel. Database → Neon (external). TLS → ACM cert on the ALB + a
Route 53 domain.

## Prerequisites

- **AWS CLI v2**, authenticated: `aws sts get-caller-identity` works.
- **Terraform ≥ 1.5**, **Docker**, **Node 20+**.
- A **Route 53 domain** (this deploy uses `llmstextgeneratorrishishah.com`; the
  backend lives at `api.<domain>`).
- **Neon** connection string (use the `-pooler` endpoint).
- Optional: OpenRouter + Bright Data credentials.

All AWS resources are defined in `terraform/`. Secrets live in
`terraform/terraform.tfvars` (gitignored — never commit it).

## 1. Configure `terraform.tfvars`

Already staged: `aws_region`, `vpc_id`, `subnet_ids`, `database_url`,
`frontend_origin`, `alarm_email`. After the domain registers, fill:

```hcl
api_domain      = "api.llmstextgeneratorrishishah.com"
route53_zone_id = "Z..."   # aws route53 list-hosted-zones
```

Get the VPC/subnets for a different account or region with:

```bash
aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query "Vpcs[0].VpcId" --output text --region us-east-2
aws ec2 describe-subnets --filters Name=vpc-id,Values=<vpc-id> \
  --query "Subnets[*].[SubnetId,AvailabilityZone]" --output table --region us-east-2
```

## 2. Bootstrap ECR + push the first image (BEFORE the full apply)

The ECS service uses a deployment circuit breaker, so a task must find its image
in ECR at create time. Create **only** the ECR repo first, push, then apply the rest.

```bash
cd terraform
terraform init
terraform apply -target=aws_ecr_repository.api    # creates only the ECR repo

ECR=$(terraform output -raw ecr_repository_url)
SHA=$(git rev-parse --short HEAD)
aws ecr get-login-password --region us-east-2 | \
  docker login --username AWS --password-stdin "${ECR%/*}"
docker build -t llms-api ../backend
docker tag llms-api:latest "$ECR:$SHA"
docker tag llms-api:latest "$ECR:latest"   # satisfies the default image_tag on first apply
docker push "$ECR:$SHA"
docker push "$ECR:latest"
```

## 3. Apply the full stack

```bash
terraform apply    # ALB, ACM (+ Route53 validation), ECS, IAM, alarms (~5-10 min)
```

The ACM cert validates automatically via the Route 53 records Terraform creates.
Confirm the **SNS subscription email** you receive (click the link) so alarms fire.

## 4. Point the frontend at the backend

```bash
API=$(terraform output -raw api_url)   # https://api.llmstextgeneratorrishishah.com
```

In the Vercel dashboard set `NEXT_PUBLIC_API_URL` = that URL, then redeploy the
frontend (`cd frontend && vercel --prod --yes`). The backend's CORS already allows
the Vercel origin via `frontend_origin`.

## 5. Verify

```bash
curl "$(terraform output -raw api_url)/health"   # {"status":"ok"}
```

Then generate from the UI at the Vercel URL.

## 6. Subsequent deploys (new backend image)

Use immutable SHA tags; updating `image_tag` rolls the service automatically
(no separate `force-new-deployment` needed):

```bash
SHA=$(git rev-parse --short HEAD)
ECR=$(terraform -chdir=terraform output -raw ecr_repository_url)
docker build -t llms-api backend
docker tag llms-api:latest "$ECR:$SHA"
docker push "$ECR:$SHA"
terraform -chdir=terraform apply -var="image_tag=$SHA"
```

## Teardown

```bash
cd terraform && terraform destroy
```

## Notes / deferred

- **Local Terraform state** — fine for solo use; move to S3 + locking for a team.
- **Secrets via tfvars → env** — values land in local state; migrate to SSM
  Parameter Store when convenient.
- **SSRF guard + rate-limiting (WAF)** — must-fix before untrusted public traffic;
  the backend fetches arbitrary user-supplied URLs.
- **Local dev is unchanged:** `uvicorn app:app --port 8000 --env-file ../.env` +
  `npm run dev`. Use a separate Neon branch locally so test runs don't overwrite
  production rows.
