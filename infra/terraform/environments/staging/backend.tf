terraform {
  backend "s3" {
    bucket         = "fantasyai-terraform-state"
    key            = "staging/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "fantasyai-terraform-locks"
    encrypt        = true
  }
}
