import uuid

from django.db import connection
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken

from .models import UserSession


class TenantJWTAuthentication(JWTAuthentication):
    """JWT auth that only accepts tokens issued for the schema serving this request."""

    def get_user(self, validated_token):
        if validated_token.get("schema") != connection.schema_name:
            raise AuthenticationFailed("Token is not valid for this tenant.", code="token_tenant_mismatch")
        session_id = validated_token.get("sid")
        if not session_id:
            raise AuthenticationFailed("Session expired. Please sign in again.", code="session_missing")
        try:
            session_id = uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            raise AuthenticationFailed("Session token is invalid.", code="session_invalid")
        user = super().get_user(validated_token)
        session = UserSession.objects.filter(
            pk=session_id, user_id=user.pk, revoked_at__isnull=True
        ).first()
        if not session:
            raise AuthenticationFailed("Session has been revoked.", code="session_revoked")
        session.last_seen_at = timezone.now()
        session.save(update_fields=["last_seen_at"])
        return user


# Import this after TenantJWTAuthentication is defined. Importing DRF's
# TokenRefreshView initializes APIView, which resolves DEFAULT_AUTHENTICATION_CLASSES
# and would otherwise re-import this partially initialized module.
from rest_framework_simplejwt.views import TokenRefreshView


class ScopedTokenRefreshSerializer(TokenRefreshSerializer):
    """Refresh only the current schema's latest, unrevoked session token."""

    def validate(self, attrs):
        try:
            refresh = RefreshToken(attrs["refresh"])
        except TokenError as exc:
            raise InvalidToken(str(exc))
        if refresh.get("schema") != connection.schema_name:
            raise InvalidToken("Refresh token does not belong to this workspace.")
        session_id = refresh.get("sid")
        if not session_id:
            raise InvalidToken("Session expired. Please sign in again.")
        try:
            session_id = uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            raise InvalidToken("Session token is invalid.")
        with transaction.atomic():
            session = UserSession.objects.select_for_update().filter(
                pk=session_id,
                user_id=refresh.get(api_settings.USER_ID_CLAIM),
                refresh_jti=refresh.get("jti"),
                revoked_at__isnull=True,
            ).first()
            if not session:
                raise InvalidToken("Refresh token has been revoked or replaced.")
            data = super().validate(attrs)
            if data.get("refresh"):
                try:
                    rotated = RefreshToken(data["refresh"])
                except TokenError as exc:
                    raise InvalidToken(str(exc))
                session.refresh_jti = rotated.get("jti", "")
            session.last_seen_at = timezone.now()
            session.save(update_fields=["refresh_jti", "last_seen_at"])
            return data


class ScopedTokenRefreshView(TokenRefreshView):
    serializer_class = ScopedTokenRefreshSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "refresh_token"
