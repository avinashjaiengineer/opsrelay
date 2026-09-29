"""The EC2 deploy script's settings handling (no AWS calls)."""

import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).resolve().parent.parent / "deploy" / "ec2" / "stack.py"
spec = importlib.util.spec_from_file_location("ec2_stack", path)
stack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stack)


def test_env_file_is_merged_over_the_defaults(tmp_path):
    env = tmp_path / "deploy.env"
    env.write_text(
        "# Slack\nOPSRELAY_SLACK_CHANNEL=C123\nOPSRELAY_SLACK_BOT_TOKEN=secretsmanager:opsrelay/slack#bot\n"
        "OPSRELAY_ENVIRONMENT='aws'\n",
        encoding="utf-8",
    )
    got = stack.settings("https://sqs/q", "https://api.example", str(env), None)
    assert got["OPSRELAY_SLACK_CHANNEL"] == "C123"
    assert got["OPSRELAY_ENVIRONMENT"] == "aws"
    assert got["OPSRELAY_PUBLIC_URL"] == "https://api.example"
    assert got["OPSRELAY_ORIGIN_SECRET"] == "secretsmanager:opsrelay/origin-secret"
    assert "OPSRELAY_SLACK_USERS" not in got


@pytest.mark.parametrize("line", ["SLACK_CHANNEL=C1", 'OPSRELAY_X=a"b', "OPSRELAY_X"])
def test_env_file_rejects_what_systemd_would_misread(tmp_path, line):
    env = tmp_path / "deploy.env"
    env.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        stack.read_env(str(env))


def test_plain_secret_values_are_warned_about(tmp_path, capsys):
    env = tmp_path / "deploy.env"
    env.write_text("OPSRELAY_JIRA_API_TOKEN=abc\n", encoding="utf-8")
    stack.read_env(str(env))
    assert "OPSRELAY_JIRA_API_TOKEN is a plain value" in capsys.readouterr().out


def test_configure_writes_catalog_users_and_drop_in(tmp_path):
    users = tmp_path / "slack-users.yaml"
    users.write_text("users:\n  - {slack_id: U1, name: sam, roles: [sre]}\n", encoding="utf-8")
    env = stack.settings("https://sqs/q", "https://api.example", None, str(users))
    script = stack.configure(env | {"OPSRELAY_X": "50%"}, str(users))
    assert "cat > /opt/opsrelay/catalog.yaml" in script and "shop-api:" in script
    assert "slack_id: U1" in script
    assert 'Environment="OPSRELAY_SLACK_USERS=/opt/opsrelay/slack-users.yaml"' in script
    assert 'Environment="OPSRELAY_X=50%%"' in script  # systemd expands %
    assert script.rstrip().endswith("systemctl restart opsrelay")
