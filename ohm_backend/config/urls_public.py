"""Public-schema URLconf: root site, platform APIs, and internal endpoints."""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from apps.platform.views import domain_check

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.platform.urls")),
    path("health/", lambda request: JsonResponse({"status": "ok", "schema": "public"})),
    path("internal/domain-check/", domain_check),
]
