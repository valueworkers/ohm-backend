"""Tenant API URLconf: tenant hosts or the shared API host with X-Tenant."""
from django.contrib import admin
from django.db import connection
from django.http import JsonResponse
from django.urls import include, path


def health(request):
    return JsonResponse({"status": "ok", "schema": connection.schema_name})


urlpatterns = [
    path("health/", health),
    path("admin/", admin.site.urls),
    path("api/", include("apps.accounts.urls")),
    # Product modules mount here later, gated per tenant by Tenant.modules
]
