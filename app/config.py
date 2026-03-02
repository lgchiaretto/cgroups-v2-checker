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

    # Skopeo settings
    SKOPEO_TLS_VERIFY = os.environ.get("SKOPEO_TLS_VERIFY", "true").lower() == "true"
    SKOPEO_AUTH_FILE = os.environ.get("SKOPEO_AUTH_FILE", "")
    SKOPEO_MAX_WORKERS = int(os.environ.get("SKOPEO_MAX_WORKERS", "20"))

    # Registry authentication
    # When enabled, reads ImagePullSecrets from pods/ServiceAccounts
    # to authenticate skopeo against private registries dynamically
    USE_IMAGE_PULL_SECRETS = os.environ.get("USE_IMAGE_PULL_SECRETS", "true").lower() == "true"

    # OpenShift settings
    SKIP_SYSTEM_NAMESPACES = os.environ.get("SKIP_SYSTEM_NAMESPACES", "true").lower() == "true"

    # Report storage
    REPORT_DIR = os.environ.get("REPORT_DIR", "/app/data/reports")
