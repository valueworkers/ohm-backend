from django.db import connection
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication


# class TenantJWTAuthentication(JWTAuthentication):
#     """JWT auth that only accepts tokens issued for the schema serving this request."""

#     def get_user(self, validated_token):
#         if validated_token.get("schema") != connection.schema_name:
#             raise AuthenticationFailed("Token is not valid for this tenant.", code="token_tenant_mismatch")
#         return super().get_user(validated_token)

class TenantJWTAuthentication(JWTAuthentication):
    """User ids repeat across tenant schemas, so a token must be bound to its tenant."""

    def get_validated_token(self, raw_token):
        token = super().get_validated_token(raw_token)
        if token.get("schema") != connection.schema_name:
            raise AuthenticationFailed("Token is not valid for this tenant.", code="token_tenant_mismatch")
        return token