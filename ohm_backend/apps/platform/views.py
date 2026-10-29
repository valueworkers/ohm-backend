from datetime import timedelta
from io import BytesIO

from django.db import IntegrityError, transaction, connection
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.db.models import ProtectedError
from django.contrib.contenttypes.models import ContentType
from django.apps import apps
from django.conf import settings
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.throttling import AnonRateThrottle

from .models import (
    Feature, Plan, Subscription, Tenant, TenantProvisioning,
    Quote, QuoteTaxSettings,
)
from .serializers import (
    ApproveSerializer,
    PlanAdminSerializer,
    PlanSerializer,
    RejectSerializer,
    ReviewSerializer,
    SubscriptionSerializer,
    TenantProvisioningCreateSerializer,
    TenantProvisioningUpdateSerializer,
    TenantProfileSerializer,
    TenantProvisioningSerializer,
    TenantCreateSerializer,
    TenantUpdateSerializer,
    default_trial_plan,
    slug_problem,
    QuoteLineSerializer, QuoteSerializer, SendTestSerializer, QuoteTaxSettingsSerializer,
)
from .tasks import provision_tenant_task
from .services import send_quote, revise_quote, respond_to_quote, recalculate
from .permissions import IsPlatformAdmin

# --------------------------------------------------------------------------
# Public
# --------------------------------------------------------------------------

class PlanViewSet(viewsets.ModelViewSet):
    """
    GET (list/retrieve): anyone, active plans only.
    POST/PUT/PATCH/DELETE: ohm admin only.
    """
    pagination_class = None

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [AllowAny()]
        return [IsPlatformAdmin()]

    def _is_admin(self):
        u = self.request.user
        return bool(u and u.is_authenticated and u.is_superuser)  # keep in sync with IsPlatformAdmin

    def get_queryset(self):
        qs = Plan.objects.prefetch_related("features")
        if self._is_admin():
            return qs  # admin sees inactive plans too
        return qs.filter(is_active=True)

    def get_serializer_class(self):
        if self._is_admin():
            return PlanAdminSerializer
        return PlanSerializer

    def destroy(self, request, *args, **kwargs):
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return Response(
                {"detail": "Plan is in use by tenants. Deactivate it (is_active=false) instead."},
                status=status.HTTP_409_CONFLICT,
            )

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
class TenantRequestThrottle(AnonRateThrottle):
    scope = "tenant_request"

class TenantProvisioningViewSet(viewsets.ModelViewSet):
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    filterset_fields = ("status",)

    EDITABLE_STATUSES = (TenantProvisioning.Status.SUBMITTED,)
    DELETABLE_STATUSES = (
        TenantProvisioning.Status.SUBMITTED,
        TenantProvisioning.Status.REJECTED,
    )

    queryset = TenantProvisioning.objects.select_related(
        "applicant", "requested_plan", "tenant"
    ).order_by("-created_at")

    def get_permissions(self):
        if self.action == "create":
            return [AllowAny()]
        return [IsPlatformAdmin()]

    def get_throttles(self):
        if self.action == "create":
            return [TenantRequestThrottle()]
        return super().get_throttles()

    def get_authenticators(self):
        # Public create shouldn't fail with 401 just because a stale/invalid token
        # was attached by the frontend. Other actions still authenticate normally.
        if getattr(self, "action", None) == "create":
            return []
        return super().get_authenticators()

    def get_serializer_class(self):
        if self.action == "create":
            return TenantProvisioningCreateSerializer
        if self.action in ("update", "partial_update"):
            return TenantProvisioningUpdateSerializer
        if self.action == "reject":
            return RejectSerializer
        if self.action == "approve":
            return ApproveSerializer
        if self.action == "retry":
            return ReviewSerializer
        return TenantProvisioningSerializer

    # ----- create (public) ------------------------------------------------

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()

        return Response(
            {"id": str(instance.pk), "status": instance.status},
            status=status.HTTP_201_CREATED,
        )

    # ----- update (PUT / PATCH) -------------------------------------------

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        pk = self.get_object().pk  # applies queryset scoping + 404 for other users' requests
        try:
            with transaction.atomic():
                obj = TenantProvisioning.objects.select_for_update().get(pk=pk)
                if obj.status not in self.EDITABLE_STATUSES:
                    raise ValidationError(
                        {"status": f"A {obj.status} request can no longer be edited."}
                    )
                serializer = self.get_serializer(obj, data=request.data, partial=partial)
                serializer.is_valid(raise_exception=True)
                obj = serializer.save()
        except IntegrityError:
            raise ValidationError({"slug": "This subdomain or domain was just requested by someone else."})
        data = TenantProvisioningSerializer(obj, context=self.get_serializer_context()).data
        return Response(data)


    # ----- destroy --------------------------------------------------------

    def destroy(self, request, *args, **kwargs):
        pk = self.get_object().pk
        with transaction.atomic():
            obj = TenantProvisioning.objects.select_for_update().get(pk=pk)
            if obj.status not in self.DELETABLE_STATUSES or obj.tenant_id:
                raise ValidationError(
                    {"status": "Approved/provisioned requests can't be deleted. "
                               "Suspend or delete the tenant instead."}
                )
            obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

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
            target_plan = body.validated_data.get("requested_plan", obj.requested_plan)
            if new_status == TenantProvisioning.Status.APPROVED and not target_plan:
                target_plan = default_trial_plan()
            obj.requested_plan = target_plan
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

class TenantViewSet(viewsets.ModelViewSet):
    permission_classes = [IsPlatformAdmin]
    filterset_fields = ("status",)
    search_fields = ("name", "schema_name", "contact_email", "domains__domain")
    queryset = (
        Tenant.objects.exclude(schema_name="public")
        .prefetch_related("domains", "subscriptions__plan")
        .order_by("-created_at")
    )

    def get_serializer_class(self):
        if self.action == "create":
            return TenantCreateSerializer
        if self.action in ("update", "partial_update"):
            return TenantUpdateSerializer
        return TenantProfileSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            tenant = serializer.save()
        except IntegrityError:
            raise ValidationError({"schema_name": "A tenant with this schema or domain already exists."})
        return Response(TenantProfileSerializer(tenant, context=self.get_serializer_context()).data,
                        status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        tenant = self.get_object()
        serializer = self.get_serializer(tenant, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(TenantProfileSerializer(tenant, context=self.get_serializer_context()).data)

    def destroy(self, request, *args, **kwargs):
        tenant = self.get_object()  # "public" is excluded from the queryset, so it 404s
        if tenant.status != Tenant.Status.SUSPENDED:
            raise ValidationError({"status": "Suspend the tenant before deleting it."})
        if request.query_params.get("confirm") != tenant.schema_name:
            raise ValidationError(
                {"confirm": "Pass ?confirm=<schema_name> to confirm permanent deletion."}
            )
        tenant.delete(force_drop=True)  # drops the schema and all its data
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get", "patch"])
    def permissions(self, request, pk=None):
        """Read or update the tenant's app/module access from the platform console."""
        tenant = self.get_object()
        if request.method == "PATCH":
            modules = request.data.get("enabled_modules")
            if not isinstance(modules, list) or any(not isinstance(code, str) for code in modules):
                raise ValidationError({"enabled_modules": "Provide a list of feature codes."})
            modules = list(dict.fromkeys(code.strip() for code in modules))
            valid = set(Feature.objects.filter(is_active=True, code__in=modules).values_list("code", flat=True))
            unknown = sorted(set(modules) - valid)
            if unknown:
                raise ValidationError({"enabled_modules": {"unknown_or_inactive": unknown}})
            tenant.modules = modules
            tenant.save(update_fields=["modules"])

        features = Feature.objects.filter(is_active=True).order_by("name")
        return Response({
            "tenant_id": tenant.pk,
            "enabled_modules": tenant.modules or [],
            "app_permissions": [
                {"code": feature.code, "name": feature.name, "description": feature.description,
                 "enabled": feature.code in (tenant.modules or [])}
                for feature in features
            ],
        })

    @action(detail=True, methods=["get", "post", "delete"], url_path="object-permissions")
    def object_permissions(self, request, pk=None):
        """Manage role grants scoped to concrete objects in a tenant schema."""
        from apps.accounts.models import Role, RoleAssignment

        tenant = self.get_object()
        with schema_context(tenant.schema_name):
            if request.method == "POST":
                user_id = request.data.get("user_id")
                role_code = request.data.get("role_code")
                object_type = request.data.get("object_type", "")
                object_id = request.data.get("object_id")
                if not all((user_id, role_code, object_type, object_id)):
                    raise ValidationError({"detail": "user_id, role_code, object_type, and object_id are required."})
                try:
                    object_id = int(object_id)
                except (TypeError, ValueError):
                    raise ValidationError({"object_id": "Object IDs must be positive integers."})
                if object_id < 1:
                    raise ValidationError({"object_id": "Object IDs must be positive integers."})
                try:
                    app_label, model_name = object_type.lower().split(".", 1)
                    content_type = ContentType.objects.get(app_label=app_label, model=model_name)
                    model = content_type.model_class()
                    tenant_app_labels = {app.split(".")[-1] for app in settings.TENANT_APPS}
                    if (model is None or model._meta.proxy or model._meta.app_label not in tenant_app_labels
                            or model._meta.app_label == "accounts"):
                        raise ContentType.DoesNotExist
                except (ValueError, ContentType.DoesNotExist):
                    raise ValidationError({"detail": "The user, role, or target object was not found in this tenant."})
                try:
                    model.objects.get(pk=object_id)
                except model.DoesNotExist:
                    raise ValidationError({"detail": "The user, role, or target object was not found in this tenant."})
                User = apps.get_model("accounts", "User")
                try:
                    user = User.objects.get(pk=user_id)
                    role = Role.objects.get(code=role_code)
                except (User.DoesNotExist, Role.DoesNotExist):
                    raise ValidationError({"detail": "The user, role, or target object was not found in this tenant."})
                assignment, created = RoleAssignment.objects.get_or_create(
                    user=user, role=role, scope_type=content_type, scope_id=object_id,
                )
                return Response(self._object_permission_data(assignment), status=201 if created else 200)

            if request.method == "DELETE":
                assignment_id = request.data.get("assignment_id") or request.query_params.get("assignment_id")
                if not assignment_id:
                    raise ValidationError({"assignment_id": "This field is required."})
                deleted, _ = RoleAssignment.objects.filter(pk=assignment_id, scope_type__isnull=False).delete()
                if not deleted:
                    raise ValidationError({"assignment_id": "Object permission grant not found."})
                return Response(status=204)

            assignments = RoleAssignment.objects.filter(scope_type__isnull=False).select_related(
                "user", "role", "scope_type", "assigned_by"
            ).prefetch_related("role__permissions").order_by("id")
            object_types = []
            tenant_app_labels = {app.split(".")[-1] for app in settings.TENANT_APPS}
            for model in apps.get_models():
                if model._meta.proxy or model._meta.auto_created or not model._meta.managed:
                    continue
                if model._meta.app_label not in tenant_app_labels or model._meta.app_label == "accounts":
                    continue
                object_types.append({"code": f"{model._meta.app_label}.{model._meta.model_name}",
                                     "name": model._meta.verbose_name.title()})
            return Response({
                "tenant_id": tenant.pk,
                "object_types": sorted(object_types, key=lambda item: item["name"].lower()),
                "roles": [{"code": role.code, "name": role.name} for role in Role.objects.order_by("rank", "name")],
                "grants": [self._object_permission_data(assignment) for assignment in assignments],
            })

    @staticmethod
    def _object_permission_data(assignment):
        return {
            "assignment_id": assignment.pk,
            "user_id": assignment.user_id,
            "user_email": assignment.user.email,
            "role_code": assignment.role.code,
            "role_name": assignment.role.name,
            "object_type": f"{assignment.scope_type.app_label}.{assignment.scope_type.model}",
            "object_id": assignment.scope_id,
            "permission_codes": list(assignment.role.permissions.order_by("code").values_list("code", flat=True)),
        }


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
            Tenant.objects.filter(owners=self.request.user)
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


# Prospect quote API
class QuoteViewSet(viewsets.ModelViewSet):
    serializer_class = QuoteSerializer
    queryset = Quote.objects.prefetch_related("lines", "events").select_related("created_by", "provisioning", "tenant")

    def get_permissions(self):
        if connection.schema_name == get_public_schema_name():
            return [IsPlatformAdmin()]
        return [IsAuthenticated()]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        if connection.schema_name == get_public_schema_name():
            qs = qs.filter(tenant__isnull=True)
        else:
            qs = qs.filter(tenant=self.request.tenant)
        if self.action == "list":
            status_filter = self.request.query_params.get("status")
            if status_filter:
                qs = qs.filter(status=status_filter)
            range_days = self.request.query_params.get("range")
            if range_days and range_days.isdigit():
                qs = qs.filter(quote_date__gte=timezone.localdate() - timedelta(days=int(range_days)))
        return qs

    def perform_update(self, serializer):
        if serializer.instance.status != Quote.Status.DRAFT:
            raise ValidationError({"status": "Only draft quotes can be edited."})
        serializer.save()

    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        try:
            quote = send_quote(self.get_object(), request.user)
        except ValueError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(self.get_serializer(quote).data)

    @action(detail=True, methods=["post"], url_path="send-test")
    def send_test(self, request, pk=None):
        serializer = SendTestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            quote = send_quote(self.get_object(), request.user, test_recipients=serializer.validated_data["recipients"])
        except ValueError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(self.get_serializer(quote).data)

    @action(detail=True, methods=["post"])
    def revise(self, request, pk=None):
        try:
            quote = revise_quote(self.get_object(), request.user)
        except ValueError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response(self.get_serializer(quote).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def pdf(self, request, pk=None):
        quote = self.get_object()
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas
        except ImportError:
            return Response({"detail": "PDF rendering is unavailable; install reportlab."}, status=503)
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        y = 800
        for text in [f"Quote {quote.number} R{quote.revision}", quote.company, quote.buyer_email, f"Date: {quote.quote_date}  Valid until: {quote.valid_until}", "", *[f"{line.position}. {line.item} x {line.qty} @ {line.unit_price if line.unit_price is not None else 'On request'}" for line in quote.lines.all()], "", f"Subtotal: {quote.subtotal}", f"CGST: {quote.cgst}  SGST: {quote.sgst}  IGST: {quote.igst}", f"Total: {quote.total if quote.total is not None else 'GST not set'}", quote.notes]:
            if y < 50:
                pdf.showPage(); y = 800
            pdf.drawString(48, y, str(text)[:110]); y -= 20
        pdf.save()
        response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{quote.number}-R{quote.revision}.pdf"'
        return response


class QuoteStatsView(APIView):
    def get_permissions(self):
        if connection.schema_name == get_public_schema_name():
            return [IsPlatformAdmin()]
        return [IsAuthenticated()]

    def get(self, request):
        qs = Quote.objects.all()
        if connection.schema_name == get_public_schema_name():
            qs = qs.filter(tenant__isnull=True)
        else:
            qs = qs.filter(tenant=request.tenant)
        start = request.query_params.get("from")
        end = request.query_params.get("to")
        if start:
            qs = qs.filter(quote_date__gte=start)
        if end:
            qs = qs.filter(quote_date__lte=end)
        stats = qs.aggregate(open=Count("id", filter=Q(status="sent")), accepted=Count("id", filter=Q(status="accepted")), rejected=Count("id", filter=Q(status="rejected")), expired=Count("id", filter=Q(status="expired")), pipeline=Sum("total", filter=Q(status="sent")))
        denominator = stats["accepted"] + stats["rejected"] + stats["expired"]
        stats["win_rate"] = round(stats["accepted"] * 100 / denominator, 2) if denominator else 0
        return Response(stats)


class QuoteTaxSettingsView(APIView):
    """Get or update the seller GST state for the current schema's quote owner."""

    def get_permissions(self):
        if connection.schema_name == get_public_schema_name():
            return [IsPlatformAdmin()]
        return [IsAuthenticated()]

    def _get_settings(self, request):
        if connection.schema_name == get_public_schema_name():
            tenant = None
            defaults = {"state_code": "29", "state_name": "Karnataka"}
        else:
            tenant = request.tenant
            gstin = tenant.gst_number or ""
            seller_code = gstin[:2] if gstin[:2].isdigit() else ""
            defaults = {"state_code": seller_code, "state_name": ""}
        settings_obj, _ = QuoteTaxSettings.objects.get_or_create(tenant=tenant, defaults=defaults)
        return settings_obj, tenant

    def get(self, request):
        settings_obj, _ = self._get_settings(request)
        return Response(QuoteTaxSettingsSerializer(settings_obj).data)

    def patch(self, request):
        settings_obj, tenant = self._get_settings(request)
        serializer = QuoteTaxSettingsSerializer(settings_obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        quotes = Quote.objects.filter(status=Quote.Status.DRAFT)
        quotes = quotes.filter(tenant=tenant) if tenant else quotes.filter(tenant__isnull=True)
        for quote in quotes:
            recalculate(quote)
        return Response(serializer.data)


class PublicQuoteResponseView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, token):
        try:
            quote = Quote.objects.prefetch_related("lines").get(accept_token=token)
        except Quote.DoesNotExist:
            return Response({"detail": "Quote not found."}, status=404)
        if quote.status != Quote.Status.SENT or quote.valid_until < timezone.localdate():
            return Response({"detail": "This quote is no longer available."}, status=410)
        return Response({
            "number": quote.number,
            "revision": quote.revision,
            "company": quote.company,
            "quote_date": quote.quote_date,
            "valid_until": quote.valid_until,
            "lines": QuoteLineSerializer(quote.lines.all(), many=True).data,
            "subtotal": quote.subtotal,
            "cgst": quote.cgst,
            "sgst": quote.sgst,
            "igst": quote.igst,
            "freight": quote.freight,
            "total": quote.total,
            "notes": quote.notes,
        })

    def post(self, request, token):
        accepted = request.data.get("accepted")
        if not isinstance(accepted, bool):
            raise ValidationError({"accepted": "Must be a boolean."})
        try:
            quote = respond_to_quote(token, accepted)
        except Quote.DoesNotExist:
            return Response({"detail": "Quote not found."}, status=404)
        except ValueError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"number": quote.number, "status": quote.status})
