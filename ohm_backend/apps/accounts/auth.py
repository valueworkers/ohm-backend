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
    """JWT auth that only accepts tokens issued for the schema serving this request, or public superuser tokens."""

    def get_user(self, validated_token):
        token_schema = validated_token.get("schema")
        current_schema = connection.schema_name
        session_id = validated_token.get("sid")
        if not session_id:
            raise AuthenticationFailed("Session expired. Please sign in again.", code="session_missing")
        try:
            session_id = uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            raise AuthenticationFailed("Session token is invalid.", code="session_invalid")

        if token_schema != current_schema:
            from django_tenants.utils import get_public_schema_name, schema_context
            public_schema = get_public_schema_name()
            if token_schema == public_schema:
                with schema_context(public_schema):
                    try:
                        public_user = super().get_user(validated_token)
                    except Exception:
                        raise AuthenticationFailed("Token is not valid for this tenant.", code="token_tenant_mismatch")
                    from apps.accounts.permissions import is_root_user, is_superadmin
                    if not (public_user and (public_user.is_superuser or is_superadmin(public_user) or is_root_user(public_user))):
                        raise AuthenticationFailed("Token is not valid for this tenant.", code="token_tenant_mismatch")
                    session = UserSession.objects.filter(
                        pk=session_id, user_id=public_user.pk, revoked_at__isnull=True
                    ).first()
                    if not session:
                        raise AuthenticationFailed("Session has been revoked.", code="session_revoked")
                    session.last_seen_at = timezone.now()
                    session.save(update_fields=["last_seen_at"])

                from apps.accounts.services import AccountsService
                return AccountsService.ensure_superuser(public_user)

            raise AuthenticationFailed("Token is not valid for this tenant.", code="token_tenant_mismatch")

        user = super().get_user(validated_token)
        session = UserSession.objects.filter(
            pk=session_id, user_id=user.pk, revoked_at__isnull=True
        ).first()
        if not session:
            raise AuthenticationFailed("Session has been revoked.", code="session_revoked")
        session.last_seen_at = timezone.now()
        session.save(update_fields=["last_seen_at"])
        return user


# Avoid module-level import of TokenRefreshView here to prevent circular import
# with DRF setting DEFAULT_AUTHENTICATION_CLASSES resolving TenantJWTAuthentication.


class ScopedTokenRefreshSerializer(TokenRefreshSerializer):
    """Refresh only the current schema's latest, unrevoked session token (or public superuser token)."""

    def validate(self, attrs):
        try:
            refresh = RefreshToken(attrs["refresh"])
        except TokenError as exc:
            raise InvalidToken(str(exc))
        token_schema = refresh.get("schema")
        current_schema = connection.schema_name
        session_id = refresh.get("sid")
        if not session_id:
            raise InvalidToken("Session expired. Please sign in again.")
        try:
            session_id = uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            raise InvalidToken("Session token is invalid.")

        from django_tenants.utils import get_public_schema_name, schema_context
        public_schema = get_public_schema_name()
        schema_to_check = current_schema

        if token_schema != current_schema:
            if token_schema == public_schema:
                with schema_context(public_schema):
                    from apps.accounts.models import User
                    from apps.accounts.permissions import is_root_user, is_superadmin
                    pub_user = User.objects.filter(pk=refresh.get(api_settings.USER_ID_CLAIM)).first()
                    if pub_user and (pub_user.is_superuser or is_superadmin(pub_user) or is_root_user(pub_user)):
                        schema_to_check = public_schema
            if schema_to_check != token_schema:
                raise InvalidToken("Refresh token does not belong to this workspace.")

        with schema_context(schema_to_check):
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


class ScopedTokenRefreshView:
    @classmethod
    def as_view(cls, **initkwargs):
        from rest_framework_simplejwt.views import TokenRefreshView

        class _View(TokenRefreshView):
            serializer_class = ScopedTokenRefreshSerializer
            throttle_classes = [ScopedRateThrottle]
            throttle_scope = "refresh_token"

        return _View.as_view(**initkwargs)
