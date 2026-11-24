# prod.tfvars — fill in before running `tofu apply`
# Apply: tofu apply -var-file=prod.tfvars -var="my_ip=$(curl -s https://checkip.amazonaws.com)/32"

aws_region    = "us-east-1"
repo_url      = "https://github.com/<owner>/<repo>.git"
instance_type = "t3.medium"
key_pair_name = "alpaca-live"
