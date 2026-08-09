terraform {
  backend "s3" {
    bucket         = "playbook-terraform-state"
    key            = "prod/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "playbook-terraform-locks"
    encrypt        = true
  }
}
