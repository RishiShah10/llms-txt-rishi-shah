data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role: what ECS needs to START the task (pull image, write logs).
resource "aws_iam_role" "ecs_execution" {
  name               = "llms-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task role: what the APP can do at runtime. Scoped tight — the only AWS API the
# app uses is S3 (writing generated llms.txt to the files bucket; see the
# task_s3 policy below). Everything else (Neon/OpenRouter/Bright Data) is external.
# So an SSRF-to-IMDS would yield only PutObject/GetObject on that one bucket —
# a small, contained blast radius, not full-account credentials.
resource "aws_iam_role" "ecs_task" {
  name               = "llms-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task_s3" {
  statement {
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.files.arn}/*"]
  }
}

resource "aws_iam_role_policy" "task_s3" {
  name   = "llms-task-s3"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.task_s3.json
}
