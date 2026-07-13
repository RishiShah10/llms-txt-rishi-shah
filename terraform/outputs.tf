output "api_url" {
  value = "https://${var.api_domain}"
}

output "alb_dns_name" {
  value = aws_lb.api.dns_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "acm_certificate_arn" {
  value = aws_acm_certificate.api.arn
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "service_name" {
  value = aws_ecs_service.api.name
}

output "files_bucket" {
  value = aws_s3_bucket.files.bucket
}

output "files_base_url" {
  value = "https://${aws_s3_bucket.files.bucket}.s3.${var.aws_region}.amazonaws.com"
}
