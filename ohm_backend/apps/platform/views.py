from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Plan, Subscription, Tenant, TenantProvisioning
from .serializers import (
    PlanSerializer,
    RejectSerializer,
    ReviewSerializer,
    SubscriptionSerializer,
    TenantProvisioningCreateSerializer,
    TenantProfileSerializer,
    TenantProvisioningSerializer,
    TenantSerializer,
    slug_problem,
)
from .tasks import provision_tenant_task
from .permissions import IsPlatformAdmin

# --------------------------------------------------------------------------
# Public
# --------------------------------------------------------------------------

class PlanListView(ListAPIView):
    """Plans shown on the registration form."""

    permission_classes = [AllowAny]
    serializer_class = PlanSerializer
    pagination_class = None
    queryset = Plan.objects.filter(is_active=True).prefetch_related("features")


class SlugAvailabilityView(APIView):
    """GET ?slug=pankaj -> live subdomain check for the registration form."""

    permission_classes = [AllowAny]

    def get(self, request):
        slug = request.query_params.get("slug", "").strip().lower()
        if not slug:
            raise ValidationError({"slug": "This query parameter is required."})
        problem = slug_problem(slug)
        return Response({"slug": slug, "available": problem is None, "reason": problem})


# --------------------------------------------------------------------------
# Onboarding: applicant submits, platform admin reviews in the "lobby"
# --------------------------------------------------------------------------

class TenantProvisioningViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    filterset_fields = ("status",)

    def get_permissions(self):
        if self.action in ("approve", "reject", "retry"):
            return [IsPlatformAdmin()]
        return [IsAuthenticated()]

    def get_queryset(self):
        qs = TenantProvisioning.objects.select_related("applicant", "requested_plan", "tenant")
        user = self.request.user
        if user.is_superuser or hasattr(user, "platform_admin_profile"):
            status_filter = self.request.query_params.get("status")
            return qs.filter(status=status_filter) if status_filter else qs
        return qs.filter(applicant=user)

    def get_serializer_class(self):
        if self.action == "create":
            return TenantProvisioningCreateSerializer
        if self.action == "reject":
            return RejectSerializer
        if self.action in ("approve", "retry"):
            return ReviewSerializer
        return TenantProvisioningSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            instance = serializer.save(applicant=request.user)
        except IntegrityError:
            # Lost a race against the unique open-request constraints.
            raise ValidationError({"slug": "This subdomain or domain was just requested by someone else."})
        data = TenantProvisioningSerializer(instance, context=self.get_serializer_context()).data
        return Response(data, status=status.HTTP_201_CREATED)

    # ----- review actions -------------------------------------------------

    def _review(self, request, expected_status, new_status):
        """Lock the row, check the transition, stamp the reviewer. Returns the instance."""
        pk = self.get_object().pk
        body = self.get_serializer(data=request.data)
        body.is_valid(raise_exception=True)
        with transaction.atomic():
            obj = TenantProvisioning.objects.select_for_update().get(pk=pk)
            if obj.status != expected_status:
                raise ValidationError(
                    {"status": f"Request is {obj.status}; expected {expected_status}."}
                )
            obj.status = new_status
            obj.reviewed_by = request.user
            obj.reviewed_at = timezone.now()
            if "review_notes" in body.validated_data:
                obj.review_notes = body.validated_data["review_notes"]
            obj.provision_error = ""
            obj.save()
        return obj

    def _enqueue(self, obj):
        """Provisioning creates a schema and runs migrations, so a Celery worker does it, not the request."""
        transaction.on_commit(lambda: provision_tenant_task.delay(str(obj.pk)))
        data = TenantProvisioningSerializer(obj, context=self.get_serializer_context()).data
        return Response(data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        obj = self._review(
            request, TenantProvisioning.Status.SUBMITTED, TenantProvisioning.Status.APPROVED
        )
        return self._enqueue(obj)

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        """Re-run provisioning for an approved request whose earlier attempt failed."""
        obj = self.get_object()
        if obj.status != TenantProvisioning.Status.APPROVED or not obj.provision_error:
            raise ValidationError({"status": "Only an approved request with a provisioning error can be retried."})
        return self._enqueue(obj)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        obj = self._review(
            request, TenantProvisioning.Status.SUBMITTED, TenantProvisioning.Status.REJECTED
        )
        data = TenantProvisioningSerializer(obj, context=self.get_serializer_context()).data
        return Response(data)


# --------------------------------------------------------------------------
# Platform admin: tenants and subscriptions
# --------------------------------------------------------------------------

class TenantViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsPlatformAdmin]
    serializer_class = TenantSerializer
    filterset_fields = ("status",)
    search_fields = ("name", "schema_name", "contact_email", "domains__domain")
    queryset = (
        Tenant.objects.exclude(schema_name="public")
        .prefetch_related("domains", "subscriptions__plan")
        .order_by("-created_at")
    )

    @action(detail=True, methods=["post"])
    def suspend(self, request, pk=None):
        tenant = self.get_object()
        if tenant.status != Tenant.Status.ACTIVE:
            raise ValidationError({"status": "Only active tenants can be suspended."})
        tenant.status = Tenant.Status.SUSPENDED
        tenant.save(update_fields=["status"])
        return Response(self.get_serializer(tenant).data)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        tenant = self.get_object()
        if tenant.status != Tenant.Status.SUSPENDED:
            raise ValidationError({"status": "Only suspended tenants can be re-activated."})
        tenant.status = Tenant.Status.ACTIVE
        tenant.save(update_fields=["status"])
        return Response(self.get_serializer(tenant).data)


class SubscriptionViewSet(viewsets.ModelViewSet):
    permission_classes = [IsPlatformAdmin]
    serializer_class = SubscriptionSerializer
    filterset_fields = ("tenant", "plan", "status")
    queryset = Subscription.objects.select_related("tenant", "plan").prefetch_related("features")


# --------------------------------------------------------------------------
# Tenant owner: complete the profile, then activate
# --------------------------------------------------------------------------

class MyTenantViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Tenants the caller requested. PATCH edits the profile; POST activate goes live."""

    permission_classes = [IsAuthenticated]
    serializer_class = TenantProfileSerializer
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "patch", "post", "head", "options"]
    pagination_class = None

    def get_queryset(self):
        return (
            Tenant.objects.filter(owner=self.request.user)
            .prefetch_related("domains")
            .order_by("-created_at")
        )

    def perform_update(self, serializer):
        if serializer.instance.status == Tenant.Status.SUSPENDED:
            raise PermissionDenied("This tenant is suspended. Contact support.")
        serializer.save()

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        pk = self.get_object().pk
        with transaction.atomic():
            tenant = Tenant.objects.select_for_update().get(pk=pk)
            if tenant.status != Tenant.Status.PROVISIONING:
                raise ValidationError({"status": f"Tenant is {tenant.status}; only a newly provisioned tenant can be activated."})
            missing = [
                f for f in ("name", "contact_email", "contact_phone", "timezone", "default_currency")
                if not getattr(tenant, f)
            ]
            if missing:
                raise ValidationError({f: "Required before activation." for f in missing})
            tenant.status = Tenant.Status.ACTIVE
            tenant.save(update_fields=["status"])
        return Response(self.get_serializer(tenant).data)