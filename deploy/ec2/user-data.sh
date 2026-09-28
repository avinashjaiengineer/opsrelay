#!/bin/bash
# EC2 user data (Amazon Linux 2023): install OpsRelay and run `opsrelay up` as a systemd service.
# The agents call Amazon Bedrock with the instance role's credentials; no keys on the box.
# Sign-in: set AUTH_MODE=dev and DEV_USERS to a Secrets Manager ARN holding the users file
# (`opsrelay users add`; token hashes only), and let the instance role read that secret.
# Without it the dashboard has no login: restrict port 8080 to trusted IPs in the security group.
set -euxo pipefail

REPO_URL="${REPO_URL:-https://github.com/avinashjaiengineer/opsrelay.git}"
MODEL_ID="${MODEL_ID:-global.amazon.nova-2-lite-v1:0}"
AUTH_MODE="${AUTH_MODE:-none}"
DEV_USERS="${DEV_USERS:-}"
REGION="$(TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60') \
  && curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)"

dnf install -y git python3.11 python3.11-pip

id opsrelay &>/dev/null || useradd --system --create-home --home-dir /opt/opsrelay --shell /sbin/nologin opsrelay
[ -d /opt/opsrelay/app ] || git clone "$REPO_URL" /opt/opsrelay/app
python3.11 -m venv /opt/opsrelay/venv
/opt/opsrelay/venv/bin/pip install --upgrade pip
/opt/opsrelay/venv/bin/pip install /opt/opsrelay/app
chown -R opsrelay:opsrelay /opt/opsrelay

cat > /etc/systemd/system/opsrelay.service <<EOF
[Unit]
Description=OpsRelay agents and dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=opsrelay
WorkingDirectory=/opt/opsrelay
Environment=OPSRELAY_MODEL_PROVIDER=bedrock
Environment=OPSRELAY_BEDROCK_MODEL_ID=${MODEL_ID}
Environment=OPSRELAY_AWS_REGION=${REGION}
Environment=OPSRELAY_AUTH_MODE=${AUTH_MODE}
Environment=OPSRELAY_DEV_USERS=${DEV_USERS}
ExecStart=/opt/opsrelay/venv/bin/opsrelay up --host 0.0.0.0 --no-browser
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now opsrelay
