module "bucket" {
  source      = "git@github.com:org/tf-modules.git//s3?ref=v1.0.0"
  bucket_name = "prod-data"
  versioning  = true
}

module "key" {
  source = "git@github.com:org/tf-modules.git//kms?ref=v2.1.0"
  alias  = "prod-key"
}
