import hashlib
import hmac
import re
import secrets

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,28}[a-z0-9]$")
HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def schema_for_slug(slug):
    """Return the database schema name used for a workspace slug."""
    return "t_" + slug.strip().lower().replace("-", "_")
