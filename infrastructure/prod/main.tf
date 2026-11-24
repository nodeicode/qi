data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnet" "public_prod" {
  vpc_id            = data.aws_vpc.default.id
  default_for_az    = true
  availability_zone = data.aws_availability_zones.available.names[1]
}

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023*-x86_64"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
}

# ---------------------------------------------------------------------------
# Production environment
# ---------------------------------------------------------------------------
module "prod" {
  source = "../modules/trading-ec2"

  environment      = "prod"
  vpc_id           = data.aws_vpc.default.id
  subnet_id        = data.aws_subnet.public_prod.id
  instance_type    = var.instance_type
  ami_id           = data.aws_ami.al2023.id
  repo_url         = var.repo_url
  branch           = "main"
  allowed_ssh_cidr = var.my_ip
  key_pair_name    = var.key_pair_name

  # Cron times are in America/New_York (ET); EventBridge Scheduler handles DST automatically.
  market_open_cron  = "cron(20 9 ? * MON-FRI *)" # T - 10 min before market open for warmup
  market_close_cron = "cron(0 16 ? * MON-FRI *)" # 4:00 PM ET

  tags = { Role = "trading-prod" }
}

# ---------------------------------------------------------------------------
# IAM — GitHub Actions OIDC
# NOTE: prod attaches an inline policy to the shared role
# created by the staging stack/config — apply staging first
# ---------------------------------------------------------------------------

data "aws_iam_role" "github_actions" {
  name = "github-actions-trader-deploy"
}

data "aws_iam_policy_document" "github_actions_prod" {
  statement {
    sid     = "SSMSendCommandProd"
    actions = ["ssm:SendCommand"]
    resources = [
      "arn:aws:ssm:*:*:document/AWS-RunShellScript",
      module.prod.instance_arn,
    ]
  }
  statement {
    sid       = "SSMGetInvocationProd"
    actions   = ["ssm:GetCommandInvocation"]
    resources = ["*"]
  }
  statement {
    sid       = "EC2Describe"
    actions   = ["ec2:DescribeInstances", "ssm:DescribeInstanceInformation"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_actions_prod" {
  name   = "trader-deploy-prod"
  role   = data.aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.github_actions_prod.json
}
