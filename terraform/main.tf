resource "aws_ecr_repository" "api" {
  name                 = "llms-api"
  image_tag_mutability = "MUTABLE" # 'latest' bootstrap; real deploys use immutable SHA tags by convention
  image_scanning_configuration {
    scan_on_push = true
  }
}
