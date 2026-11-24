variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "repo_url" {
  description = "SSH clone URL for the repo."
  type        = string
}

variable "instance_type" {
  type    = string
  default = "t3.medium"
}

variable "my_ip" {
  description = "Your public IP in CIDR notation (e.g. 203.0.113.42/32)."
  type        = string
}

variable "key_pair_name" {
  description = "EC2 Key Pair name. Remove once private subnet + NAT Gateway is enabled."
  type        = string
  default     = null
}
