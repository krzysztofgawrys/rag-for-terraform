variable "bucket_name" {
  type        = string
  description = "Name of the bucket"
}

variable "versioning" {
  type    = bool
  default = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
