module "vpc" {
  source      = "git@github.com:org/tf-modules.git//networking/vpc?ref=v3.0.0"
  environment = "qa"
}
