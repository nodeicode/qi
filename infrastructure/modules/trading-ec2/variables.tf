variable "environment" {
  description = "Deployment environment (staging or prod)."
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment must be 'staging' or 'prod'."
  }
}

variable "vpc_id" {
  description = "VPC to launch the EC2 instance in."
  type        = string
}

variable "subnet_id" {
  description = "Subnet to launch the EC2 instance in (private subnet recommended — SSM tunnels traffic)."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type."
  type        = string
  default     = "t3.medium"
}

variable "ami_id" {
  description = "Amazon Linux 2023 AMI ID for us-east-1. Update per region."
  type        = string
  # Keep this pinned — runnable via: aws ec2 describe-images --owners amazon \
  #   --filters 'Name=name,Values=al2023-ami-2023*-x86_64' \
  #   --query 'sort_by(Images,&CreationDate)[-1].ImageId'
}

variable "repo_url" {
  description = "SSH clone URL of the GitHub repository."
  type        = string
}

variable "branch" {
  description = "Git branch to deploy on the instance."
  type        = string
}

variable "market_open_cron" {
  description = "EventBridge Scheduler cron for instance START, expressed in America/New_York (ET). DST is handled automatically by schedule_expression_timezone. Default: 9:30 AM ET."
  type        = string
  default     = "cron(30 9 ? * MON-FRI *)"
}

variable "market_close_cron" {
  description = "EventBridge Scheduler cron for instance STOP, expressed in America/New_York (ET). DST is handled automatically by schedule_expression_timezone. Default: 4:00 PM ET."
  type        = string
  default     = "cron(00 16 ? * MON-FRI *)"
}

variable "root_volume_size_gb" {
  description = "EBS root volume size. 30 GB covers OS + uv cache + model artefacts."
  type        = number
  default     = 30
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days."
  type        = number
  default     = 30
}

variable "allowed_ssh_cidr" {
  description = "CIDR block allowed to reach port 22 (SSH). Should be your IP as x.x.x.x/32."
  type        = string
}

variable "key_pair_name" {
  description = "EC2 Key Pair name for SSH access. Required while instances are on public subnets. Set to null once private subnet + NAT Gateway is re-enabled."
  type        = string
  default     = null
}

variable "tags" {
  description = "Additional tags to apply to all resources."
  type        = map(string)
  default     = {}
}
