terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Separate local state from staging — lives at infrastructure/prod/terraform.tfstate
  backend "local" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "qi"
      ManagedBy = "opentofu"
    }
  }
}
