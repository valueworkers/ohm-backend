import hmac

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django_tenants.utils import get_public_schema_name
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import APIException, MethodNotAllowed, NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.auth import TenantTokenObtainPairSerializer
from apps.platform.permissions import IsRootUser, RootUserOrOnboardingApplicant

from .models import Tenant, Domain, TenantProvisioning
from .serializers import (
    TenantSerializer,
    OnboardingRequestSerializer,
    ProvisionTenantSerializer,
    RootSessionSerializer,
    RootOnboardingRequestSerializer,
    RootOnboardingRequestUpdateSerializer,
)
from .services import authenticate_root_user, provision_approved_request, update_onboarding_request
from .utils import issue_onboarding_token, verify_onboarding_token


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


class ProvisioningFailed(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Provisioning failed. The approved request can be retried."
    default_code = "provisioning_failed"


class TenantViewSet(viewsets.ModelViewSet):
    """Root-only CRUD for tenants. The public platform tenant is never exposed."""

    serializer_class = TenantSerializer
    permission_classes = [IsRootUser]
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return (
            Tenant.objects.exclude(schema_name=get_public_schema_name())
            .prefetch_related("domains")
            .order_by("-id")
        )

    def create(self, request, *args, **kwargs):
        serializer = ProvisionTenantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            onboarding_request = provision_approved_request(
                serializer.validated_data["onboarding_request"].pk
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        except Exception as exc:
            raise ProvisioningFailed() from exc
        response = TenantSerializer(onboarding_request.tenant, context=self.get_serializer_context()).data
        location = reverse("tenant-detail", kwargs={"pk": onboarding_request.tenant_id}, request=request)
        return Response(response, status=status.HTTP_201_CREATED, headers={"Location": location})

    def perform_destroy(self, tenant):
        if tenant.schema_name == get_public_schema_name():
            raise PermissionDenied("The public platform tenant cannot be deleted.")
        tenant.auto_drop_schema = True
        tenant.delete()


class RootLoginView(APIView):
    """Log in an active platform superuser and return JWTs."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "root_login"

    def post(self, request):
        serializer = RootSessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate_root_user(request, **serializer.validated_data)
        refresh = TenantTokenObtainPairSerializer.get_token(user)
        return Response({"access": str(refresh.access_token), "refresh": str(refresh)})


class OnboardingRequestViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Applicant and root-admin operations on the same onboarding request resource."""

    queryset = TenantProvisioning.objects.select_related("reviewed_by", "tenant")
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "onboarding_request"
    http_method_names = ["get", "post", "put", "patch", "head", "options"]

    def is_root_user(self):
        return IsRootUser().has_permission(self.request, self)

    def get_permissions(self):
        if self.action == "create":
            permission_classes = [AllowAny]
        elif self.action == "list":
            permission_classes = [IsRootUser]
        else:
            permission_classes = [RootUserOrOnboardingApplicant]
        return [permission() for permission in permission_classes]

    def get_serializer_class(self):
        if self.action == "partial_update" and self.is_root_user():
            return RootOnboardingRequestUpdateSerializer
        if self.action in {"create", "update", "partial_update"}:
            return OnboardingRequestSerializer
        if self.is_root_user():
            return RootOnboardingRequestSerializer
        return OnboardingRequestSerializer

    def get_queryset(self):
        return self.queryset.order_by("-created_at")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        access_token, token_digest = issue_onboarding_token()
        onboarding_request = serializer.save(access_token_hash=token_digest)
        response = {**OnboardingRequestSerializer(onboarding_request).data, "access_token": access_token}
        location = reverse("onboarding-request-detail", kwargs={"pk": onboarding_request.pk}, request=request)
        return Response(response, status=status.HTTP_201_CREATED, headers={"Location": location})

    def get_object(self):
        onboarding_request = get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])
        if not self.is_root_user():
            supplied_token = self.request.headers.get("X-Onboarding-Token", "")
            if not verify_onboarding_token(supplied_token, onboarding_request.access_token_hash):
                raise NotFound()
            if self.action in {"update", "partial_update"} and onboarding_request.status not in {
                TenantProvisioning.Status.SUBMITTED,
                TenantProvisioning.Status.NEEDS_INFO,
            }:
                raise ValidationError("This request can no longer be edited.")
        return onboarding_request

    def update(self, request, *args, **kwargs):
        if self.is_root_user():
            raise MethodNotAllowed("PUT")
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        if not self.is_root_user():
            return super().partial_update(request, *args, **kwargs)
        onboarding_request = self.get_object()
        serializer = RootOnboardingRequestUpdateSerializer(
            onboarding_request, data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        try:
            onboarding_request = update_onboarding_request(
                onboarding_request.pk,
                user=request.user,
                **serializer.validated_data,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(RootOnboardingRequestSerializer(onboarding_request).data)

    def perform_update(self, serializer):
        onboarding_request = serializer.save()
        if onboarding_request.status == TenantProvisioning.Status.NEEDS_INFO:
            onboarding_request.status = TenantProvisioning.Status.SUBMITTED
            onboarding_request.applicant_message = ""
            onboarding_request.save(update_fields=["status", "applicant_message", "updated_at"])
