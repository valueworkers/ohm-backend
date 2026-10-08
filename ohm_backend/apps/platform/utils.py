import hashlib
import hmac
import re
import secrets

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,28}[a-z0-9]$")
HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def schema_for_slug(slug):
    """Return the database schema name used for a workspace slug."""
    return "t_" + slug.strip().lower().replace("-", "_")


def issue_onboarding_token():
    """Return a private applicant token and the digest persisted for verification."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return token, digest


def verify_onboarding_token(token, expected_digest):
    """Compare an applicant token without a timing-dependent string comparison."""
    if not token or not expected_digest:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected_digest, digest)
