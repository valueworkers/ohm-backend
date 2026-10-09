"""Public-schema URLconf: root site, platform APIs, and internal endpoints."""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("auth/", include("rest_framework.urls")),
    path("", include("apps.platform.urls")),
    path("health/", lambda request: JsonResponse({"status": "ok", "schema": "public"}))
]
