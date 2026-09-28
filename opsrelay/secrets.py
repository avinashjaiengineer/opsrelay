"""Secrets: literal values for local development, AWS Secrets Manager in production.

A setting that holds a secret (the A2A token, the dev users file's tokens) may be either the value
itself or a Secrets Manager reference:

    arn:aws:secretsmanager:us-east-1:123456789012:secret:opsrelay/a2a-token-AbCdEf
    secretsmanager:opsrelay/a2a-token                  # a secret name in the configured region

References are fetched once with the process's IAM role and cached. Nothing is logged.
"""

from functools import lru_cache

from .config import get_settings


def is_reference(value: str) -> bool:
    return value.startswith(("arn:aws:secretsmanager:", "secretsmanager:"))


@lru_cache
def _fetch(secret_id: str, region: str) -> str:
    import boto3

    client = boto3.client("secretsmanager", region_name=region)
    return client.get_secret_value(SecretId=secret_id)["SecretString"]


def resolve(value: str) -> str:
    """The secret's value: `value` itself, or what the Secrets Manager reference points to."""
    if not value or not is_reference(value):
        return value
    secret_id = value.removeprefix("secretsmanager:")
    region = secret_id.split(":")[3] if secret_id.startswith("arn:") else get_settings().aws_region
    return _fetch(secret_id, region)


def clear_cache() -> None:
    _fetch.cache_clear()
