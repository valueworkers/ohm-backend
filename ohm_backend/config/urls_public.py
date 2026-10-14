"""Public-schema URLconf: root site, platform APIs, and internal endpoints."""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView
from apps.accounts.views import OTPPasswordResetView, OTPRequestView, UnifiedLoginView, UnifiedLogoutView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("auth/", include("rest_framework.urls")),
    path("api/auth/login/", UnifiedLoginView.as_view(), name="unified-login-public"),
    path("api/auth/logout/", UnifiedLogoutView.as_view(), name="unified-logout-public"),
    path("api/auth/otp/", OTPRequestView.as_view(), name="otp-request-public"),
    path("api/auth/password-reset/", OTPPasswordResetView.as_view(), name="otp-password-reset-public"),
    path("api/auth/refresh-tokens/", TokenRefreshView.as_view(), name="token-refresh-public"),
    path("", include("apps.platform.urls")),
    path("health/", lambda request: JsonResponse({"status": "ok", "schema": "public"}))
]
