terraform {
  backend "s3" {
    bucket         = "fantasyai-terraform-state"
    key            = "prod/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "fantasyai-terraform-locks"
    encrypt        = true
  }
}
