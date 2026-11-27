"""Unified API URLconf – single domain, schema switched by X-Tenant header."""
from django.contrib import admin
from django.db import connection
from django.http import JsonResponse
from django.urls import include, path
from apps.accounts.auth import ScopedTokenRefreshView
from apps.platform.forms import TenantAdminAuthenticationForm

admin.site.login_form = TenantAdminAuthenticationForm

from apps.accounts.public_views import (
    OTPPasswordResetView,
    OTPRequestView,
    UnifiedLoginView,
    UnifiedLogoutView,
)
from apps.accounts.tenant_views import TenantRegistrationCreateView

def health(request):
    return JsonResponse({"status": "ok", "schema": connection.schema_name})

admin.site.site_header = "O-hm admin"   # top bar on every admin page
admin.site.site_title = "O-hm admin"    # browser tab title suffix
admin.site.index_title = "O-hm admin"   # heading on the admin home page

urlpatterns = [
    path("health/", health),
    path("admin/", admin.site.urls),


    path("api/auth/registrations/", TenantRegistrationCreateView.as_view(), name="tenant-registration-create"),
    path("api/auth/", include("apps.accounts.urls")),
    path("api/billing/", include("apps.billing.urls")),
    path("api/care/", include("apps.billing.care_urls")),
    path("api/catalog/", include("apps.catalog.urls")),
    path("api/wallet/", include("apps.wallet.urls")),
    path("api/payroll/", include("apps.payroll.urls")),
    path("api/notifications/", include("apps.notifications.urls")),
    path("api/content/", include("apps.content.urls")),
    path("api/analytics/", include("apps.analytics.urls")),

    path("api/platform/", include("apps.platform.urls")),
]
