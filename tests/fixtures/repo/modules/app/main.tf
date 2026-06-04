module "storage" {
  source      = "../s3"
  bucket_name = "my-app-data"
}

resource "aws_instance" "app" {
  ami = "ami-12345678"
}
