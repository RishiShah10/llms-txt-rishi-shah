variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type    = string
  default = "production"
}

variable "api_domain" {
  description = "Custom domain for the backend, e.g. api.example.com"
  type        = string
}

variable "route53_zone_id" {
  description = "Route53 hosted zone id for api_domain (enables single-apply ACM validation + alias record)"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID for the ALB and ECS tasks"
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs for the ALB and ECS tasks (at least 2, in different Fargate-capable AZs)"
  type        = list(string)
}

variable "image_tag" {
  description = "ECR image tag to run. Use 'latest' for first bootstrap, git SHA thereafter."
  type        = string
  default     = "latest"
}

variable "alarm_email" {
  description = "Email address subscribed to the CloudWatch alarm SNS topic"
  type        = string
}

# --- Application config / secrets (injected as ECS environment) ---
variable "database_url" {
  type      = string
  sensitive = true
}

variable "openrouter_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "openrouter_model" {
  type    = string
  default = "openai/gpt-4o-mini"
}

variable "openrouter_base_url" {
  type    = string
  default = "https://openrouter.ai/api/v1"
}

variable "bright_data_cdp_url" {
  type      = string
  sensitive = true
  default   = ""
}

variable "frontend_origin" {
  description = "Allowed CORS origin (the Vercel domain)"
  type        = string
}

variable "cron_secret" {
  description = "Shared secret the EventBridge scheduler sends as X-Cron-Secret to /internal/cron/refresh"
  type        = string
  sensitive   = true
}
