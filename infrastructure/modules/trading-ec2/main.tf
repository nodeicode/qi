locals {
  name_prefix = "trader-${var.environment}"

  common_tags = merge(var.tags, {
    Project     = "qi"
    Environment = var.environment
    ManagedBy   = "opentofu"
  })
}

# ---------------------------------------------------------------------------
# IAM — EC2 instance role
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "trader" {
  name               = "${local.name_prefix}-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
  tags               = local.common_tags
}

# SSM Session Manager + RunCommand (replaces SSH)
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.trader.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Allow the instance to read its own secrets from Parameter Store
data "aws_iam_policy_document" "secrets" {
  statement {
    sid     = "ReadSecrets"
    actions = ["ssm:GetParameter", "ssm:GetParametersByPath"]
    resources = [
      # GetParametersByPath is authorized against the path itself (no wildcard)
      "arn:aws:ssm:*:*:parameter/trading/${var.environment}",
      # GetParameter (and the individual params returned) require the wildcard form
      "arn:aws:ssm:*:*:parameter/trading/${var.environment}/*"
    ]
  }
  statement {
    sid       = "DecryptSecrets"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      # TODO: do we use StringLike here instead to allow for regional endpoints?
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.*.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "secrets" {
  name   = "read-trading-secrets"
  role   = aws_iam_role.trader.id
  policy = data.aws_iam_policy_document.secrets.json
}

resource "aws_iam_instance_profile" "trader" {
  name = "${local.name_prefix}-profile"
  role = aws_iam_role.trader.name
  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# CloudWatch Logs — log group per environment + IAM permission
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "trader" {
  name              = "/trading/${var.environment}/trader"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "monitor" {
  name              = "/trading/${var.environment}/monitor"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

data "aws_iam_policy_document" "cloudwatch_logs" {
  statement {
    sid = "PutLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [
      "${aws_cloudwatch_log_group.trader.arn}:*",
      "${aws_cloudwatch_log_group.monitor.arn}:*",
    ]
  }
}

resource "aws_iam_role_policy" "cloudwatch_logs" {
  name   = "cloudwatch-logs"
  role   = aws_iam_role.trader.id
  policy = data.aws_iam_policy_document.cloudwatch_logs.json
}

# ---------------------------------------------------------------------------
# Security Group — SSH restricted to operator IP; HTTPS egress for APIs/PyPI
# TODO: remove SSH ingress rule and key_name once private subnet + NAT Gateway
#       is re-enabled; revert to SSM-only access.
# ---------------------------------------------------------------------------

resource "aws_security_group" "trader" {
  name        = "${local.name_prefix}-sg"
  description = "Trader EC2 - SSH from operator IP only; HTTPS egress"
  vpc_id      = var.vpc_id

  # SSH ingress — operator IP only
  ingress {
    description = "SSH from operator IP only"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  # HTTPS egress — Alpaca API, PyPI, GitHub, SSM agent
  egress {
    description = "HTTPS egress"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # HTTP egress — package mirrors that don't redirect to HTTPS (e.g. some AL2023 repos)
  egress {
    description = "HTTP egress"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# EC2 instance
# ---------------------------------------------------------------------------

resource "aws_instance" "trader" {
  ami                    = var.ami_id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  iam_instance_profile   = aws_iam_instance_profile.trader.name
  vpc_security_group_ids = [aws_security_group.trader.id]

  # SSH key pair — required for public subnet access.
  # TODO: set key_name = null and associate_public_ip_address = false once
  #       private subnet + NAT Gateway is re-enabled.
  key_name = var.key_pair_name

  # Public subnet — instance needs a public IP for direct internet access.
  associate_public_ip_address = true

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gb
    delete_on_termination = true
    encrypted             = true
  }

  # bootstrap.sh is a templatefile so OpenTofu fills in the environment,
  # repo URL, and branch before sending to the instance.
  user_data = base64encode(templatefile("${path.module}/../../../scripts/bootstrap.sh", {
    environment = var.environment
    repo_url    = var.repo_url
    branch      = var.branch
  }))

  # Persist instance between EventBridge start/stop cycles.
  # Use "terminate" only when you want a fresh instance on every deploy.
  instance_initiated_shutdown_behavior = "stop"

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-instance"
  })

  lifecycle {
    # Changing user_data after initial bootstrap is intentional (re-provisions
    # the instance). Set to true here if you manage deploys purely via SSM.
    ignore_changes = [user_data]
  }
}

# ---------------------------------------------------------------------------
# EventBridge Scheduler — start and stop the instance
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name_prefix}-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "scheduler_ec2" {
  statement {
    actions   = ["ec2:StartInstances", "ec2:StopInstances"]
    resources = [aws_instance.trader.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_ec2" {
  name   = "start-stop-instance"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_ec2.json
}

resource "aws_scheduler_schedule" "market_open" {
  name        = "${local.name_prefix}-market-open"
  description = "Start the trader instance before market open (ET)"

  flexible_time_window { mode = "OFF" }

  schedule_expression          = var.market_open_cron
  schedule_expression_timezone = "America/New_York"

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:startInstances"
    role_arn = aws_iam_role.scheduler.arn

    input = jsonencode({
      InstanceIds = [aws_instance.trader.id]
    })
  }
}

resource "aws_scheduler_schedule" "market_close" {
  name        = "${local.name_prefix}-market-close"
  description = "Stop the trader instance after market close (ET)"

  flexible_time_window { mode = "OFF" }

  schedule_expression          = var.market_close_cron
  schedule_expression_timezone = "America/New_York"

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    role_arn = aws_iam_role.scheduler.arn

    input = jsonencode({
      InstanceIds = [aws_instance.trader.id]
    })
  }
}
