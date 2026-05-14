"""Application configuration."""

import os
import secrets


def get_or_create_secret_key():
    """Get secret key from environment or generate one."""
    secret_key = os.environ.get("SECRET_KEY")
    if secret_key:
        return secret_key
    return secrets.token_hex(32)


class Config:
    """Flask application configuration."""

    SECRET_KEY = get_or_create_secret_key()

    # OpenShift settings
    SKIP_SYSTEM_NAMESPACES = os.environ.get("SKIP_SYSTEM_NAMESPACES", "true").lower() == "true"

    # Report storage
    REPORT_DIR = os.environ.get("REPORT_DIR", "/app/data/reports")
