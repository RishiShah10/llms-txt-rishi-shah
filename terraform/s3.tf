resource "aws_s3_bucket" "files" {
  bucket = "llmstextgeneratorrishishah-files"
}

resource "aws_s3_bucket_public_access_block" "files" {
  bucket                  = aws_s3_bucket.files.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false # we attach a public-read bucket policy below
  restrict_public_buckets = false
}

data "aws_iam_policy_document" "files_public_read" {
  statement {
    sid       = "PublicRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.files.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_s3_bucket_policy" "files" {
  bucket     = aws_s3_bucket.files.id
  policy     = data.aws_iam_policy_document.files_public_read.json
  depends_on = [aws_s3_bucket_public_access_block.files]
}
