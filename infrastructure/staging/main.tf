data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnet" "public_staging" {
  vpc_id            = data.aws_vpc.default.id
  default_for_az    = true
  availability_zone = data.aws_availability_zones.available.names[0]
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

module "staging" {
  source = "../modules/trading-ec2"

  environment      = "staging"
  vpc_id           = data.aws_vpc.default.id
  subnet_id        = data.aws_subnet.public_staging.id
  instance_type    = var.instance_type
  ami_id           = data.aws_ami.al2023.id
  repo_url         = var.repo_url
  branch           = "staging"
  allowed_ssh_cidr = var.my_ip
  key_pair_name    = var.key_pair_name

  market_open_cron  = "cron(20 9 ? * MON-FRI *)" # T - 10 min before market open for warmup
  market_close_cron = "cron(0 16 ? * MON-FRI *)" # 4:00 PM ET

  tags = { Role = "trading-staging" }
}

# ---------------------------------------------------------------------------
# IAM — GitHub Actions OIDC trust (shared; prod root attaches an extra policy)
# ---------------------------------------------------------------------------

# GitHub OIDC IdP — created once via CLI (see infrastructure/README.md);
# read here, not managed (thumbprints left as created — they were working).
# sub claim: this repo moved to IMMUTABLE subject claims when it was renamed
# (github.blog/changelog/2026-04-23) — the sub now embeds owner and repo IDs
# (repo:<owner>/<repo>@<repo-id>), so the trust condition
# below uses the IDs, NOT the mutable repo name.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

data "aws_iam_policy_document" "github_actions_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      # Immutable subject claim (owner/repo IDs — rename-safe).
      values = ["repo:<owner>/<repo>@<repo-id>:*"]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name               = "github-actions-trader-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume.json
  tags               = { ManagedBy = "opentofu" }
}

data "aws_iam_policy_document" "github_actions_deploy" {
  statement {
    sid     = "SSMSendCommand"
    actions = ["ssm:SendCommand"]
    resources = [
      "arn:aws:ssm:*:*:document/AWS-RunShellScript",
      module.staging.instance_arn,
    ]
  }
  statement {
    sid       = "SSMGetInvocation"
    actions   = ["ssm:GetCommandInvocation"]
    resources = ["*"]
  }
  statement {
    sid       = "EC2Describe"
    actions   = ["ec2:DescribeInstances", "ssm:DescribeInstanceInformation"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_actions_deploy" {
  name   = "trader-deploy-staging"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.github_actions_deploy.json
}
