"""Unified API URLconf – single domain, schema switched by X-Tenant header."""
from django.contrib import admin
from django.db import connection
from django.http import JsonResponse
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts.views import (
    OTPPasswordResetView,
    OTPRequestView,
    TenantRegistrationCreateView,
    UnifiedLoginView,
    UnifiedLogoutView,
)

def health(request):
    return JsonResponse({"status": "ok", "schema": connection.schema_name})


urlpatterns = [
    path("health/", health),
    path("admin/", admin.site.urls),

    # --- Auth (works on any schema – public or tenant) ---
    path("api/auth/login/", UnifiedLoginView.as_view(), name="unified-login"),
    path("api/auth/logout/", UnifiedLogoutView.as_view(), name="unified-logout"),
    path("api/auth/otp/", OTPRequestView.as_view(), name="otp-request"),
    path("api/auth/password-reset/", OTPPasswordResetView.as_view(), name="otp-password-reset"),
    path("api/auth/access-tokens/", TokenRefreshView.as_view(), name="token-refresh"),

    # --- Tenant-scoped (only meaningful with X-Tenant) ---
    path("api/auth/registrations/", TenantRegistrationCreateView.as_view(), name="tenant-registration-create"),
    path("api/", include("apps.accounts.urls")),

    # --- Platform admin + onboarding (public schema) ---
    path("api/platform/", include("apps.platform.urls")),
]
