import hmac

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django_tenants.utils import get_public_schema_name
from rest_framework import mixins, viewsets
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError, NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.reverse import reverse
from apps.platform.permissions import IsRootUser

from .models import Tenant, Domain, TenantProvisioning, Feature
from .serializers import (
    TenantSerializer,
    TenantRequestCreateSerializer,
    TenantRequestSerializer,
    RootTenantRequestSerializer,
    TenantRequestApprovalSerializer,
    HomeFeatureSerializer,
)
from .services import review_onboarding_request
from .utils import SLUG_RE

def domain_check(request):
    """Internal edge-proxy check: return success only for verified domains."""
    if connection.schema_name != get_public_schema_name():
        return JsonResponse({"ok": False}, status=404)
    supplied = request.GET.get("token", "")
    expected = settings.INTERNAL_TOKEN
    if not expected or not hmac.compare_digest(supplied, expected):
        return JsonResponse({"ok": False}, status=403)
    domain = request.GET.get("domain", "").strip().lower()
    exists = Domain.objects.filter(domain=domain, verified=True).exists()
    return JsonResponse({"ok": exists}, status=200 if exists else 404)


class TenantViewSet(viewsets.ReadOnlyModelViewSet):
    """Root-only read access to provisioned tenants."""

    serializer_class = TenantSerializer
    permission_classes = [IsRootUser]

    def get_queryset(self):
        return (
            Tenant.objects.exclude(schema_name=get_public_schema_name())
            .prefetch_related("domains")
            .order_by("-id")
        )

class TenantRequestViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    POST   /tenant-requests/        public signup (creates public user + request)
    GET    /tenant-requests/        root: the lobby (?status=submitted); applicant: own requests
    GET    /tenant-requests/{id}/   root or the owning applicant
    PATCH  /tenant-requests/{id}/   root only: approve / reject
    """

    http_method_names = ["get", "post", "patch", "head", "options"]
    throttle_scope = "tenant_request"

    def get_throttles(self):
        return [ScopedRateThrottle()] if self.action == "create" else []

    def get_permissions(self):
        if self.action == "create":
            return [AllowAny()]
        if self.action == "partial_update":
            return [IsRootUser()]
        return [IsAuthenticated()]

    def is_root_user(self):
        return IsRootUser().has_permission(self.request, self)

    def get_serializer_class(self):
        if self.action == "create":
            return TenantRequestCreateSerializer
        if self.action == "partial_update":
            return TenantRequestApprovalSerializer
        return RootTenantRequestSerializer if self.is_root_user() else TenantRequestSerializer

    def get_queryset(self):
        qs = TenantProvisioning.objects.select_related("applicant", "reviewed_by", "tenant").order_by("-created_at")
        if not self.is_root_user():
            return qs.filter(applicant=self.request.user)
        status_filter = self.request.query_params.get("status")
        return qs.filter(status=status_filter) if status_filter else qs

    def partial_update(self, request, *args, **kwargs):
        tenant_request = self.get_object()
        serializer = TenantRequestApprovalSerializer(tenant_request, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            updated = review_onboarding_request(
                tenant_request.pk, user=request.user, **serializer.validated_data
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(RootTenantRequestSerializer(updated).data)