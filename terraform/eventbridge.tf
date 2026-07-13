# Nightly auto-update sweep: EventBridge POSTs directly to the API at midnight
# UTC -- no Lambda, no extra compute. The connection stores the shared secret
# (in Secrets Manager, managed by EventBridge) and sends it as X-Cron-Secret;
# the endpoint runs the sweep as a background task inside the ECS service.

resource "aws_cloudwatch_event_connection" "cron" {
  name               = "llms-cron"
  description        = "Auth header for the llms.txt refresh endpoint"
  authorization_type = "API_KEY"

  auth_parameters {
    api_key {
      key   = "X-Cron-Secret"
      value = var.cron_secret
    }
  }
}

resource "aws_cloudwatch_event_api_destination" "refresh" {
  name                             = "llms-refresh"
  description                      = "POST /internal/cron/refresh on the live API"
  invocation_endpoint              = "https://${var.api_domain}/internal/cron/refresh"
  http_method                      = "POST"
  invocation_rate_limit_per_second = 1
  connection_arn                   = aws_cloudwatch_event_connection.cron.arn
}

# EventBridge needs an explicit role to invoke API destinations.
resource "aws_iam_role" "eventbridge_invoke" {
  name = "llms-eventbridge-invoke"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "eventbridge_invoke" {
  name = "invoke-api-destination"
  role = aws_iam_role.eventbridge_invoke.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "events:InvokeApiDestination"
      Resource = aws_cloudwatch_event_api_destination.refresh.arn
    }]
  })
}

# Midnight UTC daily -- matches the day-granular recrawl intervals, which snap
# next_check_at to UTC midnights.
resource "aws_cloudwatch_event_rule" "nightly_refresh" {
  name                = "llms-nightly-refresh"
  description         = "Trigger the auto-update sweep at 00:00 UTC"
  schedule_expression = "cron(0 0 * * ? *)"
}

resource "aws_cloudwatch_event_target" "nightly_refresh" {
  rule     = aws_cloudwatch_event_rule.nightly_refresh.name
  arn      = aws_cloudwatch_event_api_destination.refresh.arn
  role_arn = aws_iam_role.eventbridge_invoke.arn

  # The endpoint reads the header, not the body; retries cover transient
  # ALB/deploy blips. A missed night self-heals: sites stay due until swept.
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 3
  }
}
