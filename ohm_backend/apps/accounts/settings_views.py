import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from .models import EmailVerification, UserSettings
from .services import AccountsService


CHANNELS = ("email", "in_app", "browser_push", "mobile_push", "sms")
CATEGORIES = ("product_updates", "marketing_emails", "security_alerts", "billing_alerts")
THEMES = ("light", "dark", "system")
FONT_SIZES = ("small", "medium", "large")
FONT_FAMILIES = ("system", "inter", "roboto", "open_sans", "lato")
DEFAULT_CHANNELS = {key: key in ("email", "in_app") for key in CHANNELS}
DEFAULT_CATEGORIES = {key: key in ("security_alerts", "billing_alerts") for key in CATEGORIES}


class SettingsEmailThrottle(UserRateThrottle):
    scope = "settings_email"


def user_settings(user):
    return UserSettings.objects.get_or_create(user=user)[0]


def profile_data(user, pending_email=None):
    return {
        "id": user.pk,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "email_verified": user.email_verified_at is not None,
        "email_verified_at": user.email_verified_at,
        "pending_email": pending_email,
    }


class SettingsProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        pending = EmailVerification.objects.filter(user=request.user, consumed_at__isnull=True, expires_at__gt=timezone.now()).order_by("-created_at").first()
        return Response(profile_data(request.user, pending.email if pending else None))

    def patch(self, request):
        unknown = set(request.data) - {"first_name", "last_name"}
        if unknown:
            raise serializers.ValidationError({field: "Use the dedicated email change endpoint for email updates." for field in unknown})
        fields = {}
        for name in ("first_name", "last_name"):
            if name in request.data:
                value = request.data[name]
                if not isinstance(value, str):
                    raise serializers.ValidationError({name: "Must be a string."})
                value = value.strip()
                if len(value) > 30:
                    raise serializers.ValidationError({name: "Must be 30 characters or fewer."})
                fields[name] = value
        for name, value in fields.items():
            setattr(request.user, name, value)
        if fields:
            request.user.save(update_fields=list(fields))
        return Response(profile_data(request.user))


class EmailChangeView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [SettingsEmailThrottle]

    def post(self, request):
        email = serializers.EmailField()(request.data.get("email", ""))
        email = email.strip().lower()
        if email == request.user.email.lower():
            raise serializers.ValidationError({"email": "This is already your email address."})
        self._send(request.user, email)
        return Response({"detail": "Verification email sent."}, status=status.HTTP_202_ACCEPTED)

    @staticmethod
    def _send(user, email):
        if user.__class__.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
            raise serializers.ValidationError({"email": "An account with this email already exists."})
        raw_token = secrets.token_urlsafe(32)
        EmailVerification.objects.filter(user=user, consumed_at__isnull=True).update(consumed_at=timezone.now())
        EmailVerification.objects.create(
            user=user, email=email, token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(minutes=30),
        )
        send_mail(
            "Verify your OHM email address",
            f"Use this one-time verification code to confirm {email}:\n\n{raw_token}\n\nIt expires in 30 minutes.",
            settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False,
        )


class EmailResendView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [SettingsEmailThrottle]

    def post(self, request):
        pending = EmailVerification.objects.filter(user=request.user, consumed_at__isnull=True, expires_at__gt=timezone.now()).order_by("-created_at").first()
        if not pending:
            raise serializers.ValidationError({"detail": "There is no pending email change to resend."})
        EmailChangeView._send(request.user, pending.email)
        return Response({"detail": "Verification email sent."}, status=status.HTTP_202_ACCEPTED)


class EmailVerifyView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [SettingsEmailThrottle]

    def post(self, request):
        token = request.data.get("token")
        if not isinstance(token, str) or not token:
            raise serializers.ValidationError({"token": "This field is required."})
        digest = hashlib.sha256(token.encode()).hexdigest()
        try:
            with transaction.atomic():
                verification = EmailVerification.objects.select_for_update().get(
                    user=request.user, token_hash=digest, consumed_at__isnull=True, expires_at__gt=timezone.now()
                )
                if request.user.__class__.objects.filter(email__iexact=verification.email).exclude(pk=request.user.pk).exists():
                    raise serializers.ValidationError({"email": "An account with this email already exists."})
                old_email = request.user.email
                request.user.email = verification.email
                request.user.email_verified_at = timezone.now()
                request.user.save(update_fields=["email", "email_verified_at"])
                try:
                    AccountsService.sync_owner_email(
                        request.user, old_email, request.user.email, request.user.email_verified_at
                    )
                except ValueError as exc:
                    raise serializers.ValidationError({"email": str(exc)})
                verification.consumed_at = timezone.now()
                verification.save(update_fields=["consumed_at"])
        except EmailVerification.DoesNotExist:
            raise serializers.ValidationError({"token": "Invalid, expired, or already used token."})
        except IntegrityError:
            raise serializers.ValidationError({"email": "An account with this email already exists."})
        return Response(profile_data(request.user))


class AppearanceSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        pref = user_settings(request.user)
        return Response({"theme": pref.theme, "font_size": pref.font_size, "font_family": pref.font_family})

    def patch(self, request):
        pref = user_settings(request.user)
        for name, allowed in (("theme", THEMES), ("font_size", FONT_SIZES), ("font_family", FONT_FAMILIES)):
            if name in request.data:
                value = request.data[name]
                if not isinstance(value, str) or value not in allowed:
                    raise serializers.ValidationError({name: f"Choose one of: {', '.join(allowed)}."})
                setattr(pref, name, value)
        pref.save()
        return self.get(request)


class NotificationSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        pref = user_settings(request.user)
        channels = {**DEFAULT_CHANNELS, **pref.notification_channels}
        categories = {**DEFAULT_CATEGORIES, **pref.notification_categories}
        categories["security_alerts"] = True
        categories["billing_alerts"] = True
        return Response({"channels": channels, "categories": categories, "marketing_consent_at": pref.marketing_consent_at})

    def patch(self, request):
        pref = user_settings(request.user)
        for field, allowed in (("channels", CHANNELS), ("categories", CATEGORIES)):
            if field not in request.data:
                continue
            updates = request.data[field]
            if not isinstance(updates, dict):
                raise serializers.ValidationError({field: "Must be an object of boolean preferences."})
            unknown = set(updates) - set(allowed)
            if unknown:
                raise serializers.ValidationError({field: f"Unsupported preferences: {', '.join(sorted(unknown))}."})
            if any(type(value) is not bool for value in updates.values()):
                raise serializers.ValidationError({field: "All preference values must be booleans."})
            target = "notification_channels" if field == "channels" else "notification_categories"
            setattr(pref, target, {**getattr(pref, target), **updates})
            if field == "categories" and "marketing_emails" in updates:
                pref.marketing_consent_at = timezone.now() if updates["marketing_emails"] else None
        if "categories" in request.data:
            pref.notification_categories["security_alerts"] = True
            pref.notification_categories["billing_alerts"] = True
        pref.save()
        return self.get(request)
