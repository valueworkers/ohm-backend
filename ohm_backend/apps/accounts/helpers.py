import secrets
import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMultiAlternatives, send_mail
from django.db import connection
from django.utils.module_loading import import_string
from django.template.loader import render_to_string

User = get_user_model()

OTP_TTL_SECONDS = 300
OTP_MAX_ATTEMPTS = 5

OTP_EMAIL_CONFIG = {
    "password_reset": ("Your OHM password reset code", "password_reset_otp.html"),
    "login":          ("Your OHM login code",          "otp-login.html"),
    "signup":         ("Verify your OHM account",      "otp-login.html"),
}
DEFAULT_OTP_EMAIL = ("Your OHM verification code", "otp-login.html")

OTP_EXPIRY_MINUTES = OTP_TTL_SECONDS // 60


# ---------- user lookup / eligibility ----------

def find_user(username):
    """Resolve a user by email (contains '@') or phone. Returns None if missing or ambiguous."""
    username = username.strip()
    if "@" in username:
        matches = User.objects.filter(email__iexact=username)
    else:
        matches = User.objects.filter(phone=username)
    return matches.first() if matches.count() == 1 else None


def has_password(user):
    """True only if the user has a real, usable password (not None, '' or '!...')."""
    return bool(user.password) and user.has_usable_password()


def is_eligible(user):
    """Public schema: platform admins only. Tenant schemas: any user."""
    if connection.schema_name == "public":
        return user.is_superuser or hasattr(user, "platform_admin_profile")
    return True


# ---------- OTP ----------
def _otp_key(user, purpose):
    # User IDs repeat across schemas, so include the active schema in the key.
    return f"otp:{connection.schema_name}:{user.pk}:{purpose}"


def _send_otp(user, username, code, purpose):
    if "@" in username:
        subject, template = OTP_EMAIL_CONFIG.get(purpose, DEFAULT_OTP_EMAIL)
        purpose_display = purpose.replace("_", " ")
        context = {
            "code": code,
            "purpose": purpose,
            "purpose_display": purpose_display,
            "expiry_minutes": OTP_EXPIRY_MINUTES,
        }

        msg = EmailMultiAlternatives(
            subject=subject,
            body=f"Your {purpose_display} code is {code}. It expires in {OTP_EXPIRY_MINUTES} minutes.",
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            to=[user.email],
        )
        msg.attach_alternative(render_to_string(template, context), "text/html")
        msg.send(fail_silently=False)
        return

    backend_path = getattr(settings, "OHM_SMS_OTP_BACKEND", "")
    if not backend_path:
        raise ImproperlyConfigured("Configure OHM_SMS_OTP_BACKEND to send OTPs to phone numbers.")
    backend = import_string(backend_path)
    backend(phone=user.phone, code=code, purpose=purpose)

def issue_otp(user, username, purpose):
    """Generate, store and send an OTP. Clears the stored challenge if sending fails."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    key = _otp_key(user, purpose)
    cache.set(
        key,
        {"code_hash": make_password(code), "attempts": 0, "expires_at": time.time() + OTP_TTL_SECONDS},
        timeout=OTP_TTL_SECONDS,
    )
    try:
        _send_otp(user, username, code, purpose)
    except Exception:
        cache.delete(key)
        raise


def _challenge_usable(challenge):
    return (
        challenge is not None
        and challenge["attempts"] < OTP_MAX_ATTEMPTS
        and challenge["expires_at"] > time.time()
    )


def consume_otp(user, purpose, code):
    """Verify an OTP. Single use, attempt-limited, and guarded against concurrent verifies."""
    key = _otp_key(user, purpose)
    if not _challenge_usable(cache.get(key)):
        return False

    lock_key = f"{key}:verify-lock"
    if not cache.add(lock_key, "1", timeout=5):
        return False
    try:
        challenge = cache.get(key)
        if not _challenge_usable(challenge):
            return False
        if check_password(code, challenge["code_hash"]):
            cache.delete(key)
            return True
        challenge["attempts"] += 1
        cache.set(key, challenge, timeout=max(1, int(challenge["expires_at"] - time.time())))
        return False
    finally:
        cache.delete(lock_key)