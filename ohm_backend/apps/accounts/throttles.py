import hashlib

from rest_framework.throttling import SimpleRateThrottle


class AuthIdentityThrottle(SimpleRateThrottle):
    """Limit anonymous authentication attempts by normalized account identity."""

    scope = "login_identity"

    def get_cache_key(self, request, view):
        identity = request.data.get("username", request.data.get("email", ""))
        if not isinstance(identity, str) or not identity.strip():
            return None
        digest = hashlib.sha256(identity.strip().lower().encode()).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": digest}


class OTPIdentityThrottle(AuthIdentityThrottle):
    """Apply an independent per-identity limit to OTP requests and checks."""

    scope = "otp_identity"
