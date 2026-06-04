output "bucket_arn" {
  description = "ARN of the bucket"
  value       = aws_s3_bucket.this.arn
}

output "bucket_id" {
  value     = aws_s3_bucket.this.id
  sensitive = true
}
