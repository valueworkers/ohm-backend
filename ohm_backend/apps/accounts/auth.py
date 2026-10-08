from django.db import connection
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


class TenantTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["schema"] = connection.schema_name
        return token


class TenantJWTAuthentication(JWTAuthentication):
    """User ids repeat across tenant schemas, so a token must be bound to its tenant."""

    def get_validated_token(self, raw_token):
        token = super().get_validated_token(raw_token)
        if token.get("schema") != connection.schema_name:
            raise InvalidToken("Token does not belong to this workspace")
        return token
