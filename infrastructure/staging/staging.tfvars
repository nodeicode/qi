# Apply: tofu apply -var-file=staging.tfvars -var="my_ip=$(curl -s https://checkip.amazonaws.com)/32"

aws_region    = "us-east-1"
repo_url      = "https://github.com/<owner>/<repo>.git"

# Instance: dedicated-vCPU compute (stage 4; upgrade from burstable t3.medium).
# Applies in place (stop -> modify -> start, per the plan): same instance id and
# EBS volume, so broker state and CloudWatch log streams are preserved.
instance_type = "c7i.large"
key_pair_name = "alpaca-paper"
