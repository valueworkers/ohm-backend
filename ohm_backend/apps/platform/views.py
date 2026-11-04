from io import BytesIO
from rest_framework.filters import OrderingFilter, SearchFilter
from . import gst
from .filters import InvoiceFilter, QuoteFilter, latest_revisions
from . import services

from django.db.models import Count, DecimalField, F, Q, Sum, Value, ProtectedError
from django.db.models.functions import Coalesce
from django.db import IntegrityError, transaction, connection
from django.http import HttpResponse
from django.contrib.contenttypes.models import ContentType
from django.apps import apps
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.urls import reverse
from django_filters.rest_framework import DjangoFilterBackend
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.throttling import AnonRateThrottle
from decimal import Decimal
from .models import (
    Feature, Plan, Subscription, Tenant, TenantProvisioning,
    Quote, QuoteTaxSettings, QuoteLine, Invoice, InvoiceItem, Payment
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
    QuoteLineSerializer, QuoteSerializer,
    SendTestSerializer, QuoteTaxSettingsSerializer,
    InvoiceListSerializer, InvoiceSerializer,
    PaymentReadSerializer, RecordPaymentSerializer,
)
from .tasks import provision_tenant_task
from .services import send_quote, revise_quote, respond_to_quote, recalculate
from .permissions import IsPlatformAdmin


SUPPLY_LABELS = {
    gst.INTRA_STATE: "CGST + SGST", gst.INTRA_UT: "CGST + UTGST",
    gst.INTER_STATE: "IGST", gst.UNKNOWN: "Unknown",
}
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


class QuoteViewSet(viewsets.ModelViewSet):
    serializer_class = QuoteSerializer
    queryset = Quote.objects.prefetch_related("lines", "events").select_related("created_by", "provisioning", "tenant")
    http_method_names = ["get", "post", "patch", "head", "options"]

    # Built-in filtering, search and ordering
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = QuoteFilter
    search_fields = [
        "number", "company", "attention", "gstin", "buyer_email", "your_ref", "state",
        "lines__item", "lines__hsn_sac",
    ]
    ordering_fields = [
        "quote_date", "number", "revision", "company", "status", "state", "subtotal", "taxable_amount",
        "total", "sent_at", "accepted_at", "created_at",
    ]
    ordering = ["-quote_date", "-number", "-revision"]

    def get_permissions(self):
        if connection.schema_name == get_public_schema_name():
            return [IsPlatformAdmin()]
        return [IsAuthenticated()]

    def get_queryset(self):
        qs = super().get_queryset()
        if connection.schema_name == get_public_schema_name():
            return qs.filter(tenant__isnull=True)
        return qs.filter(tenant=self.request.tenant)

    def perform_update(self, serializer):
        if serializer.instance.status != Quote.Status.DRAFT:
            raise ValidationError({"status": "Only draft quotes can be edited."})
        serializer.save()

    # ---- helpers for the form ----
    @action(detail=False, methods=["get"])
    def meta(self, request):
        """Dropdown data: states, UOMs, GST slabs, line kinds."""
        return Response({
            "states": [{"code": c, "name": n} for c, n in sorted(gst.STATE_CODES.items(), key=lambda kv: kv[1])],
            "uoms": [{"value": v, "label": l} for v, l in QuoteLine.Uom.choices],
            "gst_rates": [str(r) for r in gst.GST_RATES],
            "line_kinds": [{"value": v, "label": l} for v, l in QuoteLine.Kind.choices],
            "default_terms": [],
        })

    @action(detail=False, methods=["get"], url_path="gstin-info")
    def gstin_info(self, request):
        """Validate a GSTIN offline and say which GST applies against the seller's state."""
        gstin = request.query_params.get("gstin", "").strip().upper()
        if not gst.is_valid_gstin(gstin):
            raise ValidationError({"gstin": "Not a valid GSTIN (format or check digit)."})
        tenant_id = None if connection.schema_name == get_public_schema_name() else request.tenant.pk
        seller_code, seller_name, _ = services.get_seller_tax_settings(tenant_id)
        code = gst.state_code_from_gstin(gstin)
        stype = gst.supply_type(seller_code, code)
        return Response({
            "gstin": gstin, "pan": gst.pan_from_gstin(gstin),
            "state_code": code, "state": gst.STATE_CODES[code],
            "supply_type": stype, "tax": SUPPLY_LABELS[stype],
            "seller_state": seller_name,
        })

    # ---- actions ----
    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        try:
            quote_obj = self.get_object()
            response_url = request.build_absolute_uri(reverse("public-quote-response", kwargs={"token": quote_obj.accept_token}))
            quote = send_quote(quote_obj, request.user, response_url=response_url)
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
        rows = [f"Quote {quote.number} R{quote.revision}", quote.company, f"GSTIN: {quote.gstin or '-'}  Place of supply: {quote.state} ({quote.place_of_supply_code})",
                quote.buyer_email, f"Date: {quote.quote_date}  Valid until: {quote.valid_until}", ""]
        for line in quote.lines.all():
            price = "On request" if line.unit_price is None else line.unit_price
            rows.append(f"{line.position}. {line.item} [HSN/SAC {line.hsn_sac}] {line.qty} {line.uom} @ {price}  disc {line.discount_percent}%  GST {line.gst_rate}%  = {line.line_total}")
        rows += ["", f"Subtotal: {quote.subtotal}  Discount: {quote.discount_total}  Taxable: {quote.taxable_amount}"]
        if quote.supply_type == Quote.SupplyType.INTER_STATE:
            rows.append(f"IGST: {quote.igst}")
        else:
            rows.append(f"CGST: {quote.cgst}  {quote.state_tax_label}: {quote.sgst}")
        rows += [f"Round off: {quote.round_off}", f"Total: {quote.total if quote.total is not None else 'GST not set'}",
                "", quote.notes, "", *quote_terms(quote)]
        for text in rows:
            if y < 50:
                pdf.showPage(); y = 800
            pdf.drawString(48, y, str(text)[:110]); y -= 18
        pdf.save()
        response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{quote.number}-R{quote.revision}.pdf"'
        return response


class QuoteStatsView(APIView):
    """Pipeline numbers over the latest revision of each quote. Accepts the same filters as the list."""

    def get_permissions(self):
        if connection.schema_name == get_public_schema_name():
            return [IsPlatformAdmin()]
        return [IsAuthenticated()]

    def get(self, request):
        qs = latest_revisions(Quote.objects.all())
        if connection.schema_name == get_public_schema_name():
            qs = qs.filter(tenant__isnull=True)
        else:
            qs = qs.filter(tenant=request.tenant)
        for param, lookup in (("from", "quote_date__gte"), ("to", "quote_date__lte")):   # kept for the existing UI
            raw = request.query_params.get(param)
            if raw:
                parsed = parse_date(raw)
                if parsed is None:
                    raise ValidationError({param: "Use a valid date in YYYY-MM-DD format."})
                qs = qs.filter(**{lookup: parsed})
        filterset = QuoteFilter(request.query_params, queryset=qs)
        if not filterset.is_valid():
            raise ValidationError(filterset.errors)
        qs = filterset.qs

        money = DecimalField(max_digits=14, decimal_places=2)
        zero = Value(Decimal("0.00"), output_field=money)
        stats = qs.aggregate(
            draft=Count("id", filter=Q(status="draft")),
            open=Count("id", filter=Q(status="sent")),
            accepted=Count("id", filter=Q(status="accepted")),
            rejected=Count("id", filter=Q(status="rejected")),
            expired=Count("id", filter=Q(status="expired")),
            pipeline=Coalesce(Sum("total", filter=Q(status="sent")), zero, output_field=money),
            won_value=Coalesce(Sum("total", filter=Q(status="accepted")), zero, output_field=money),
            won_igst=Coalesce(Sum("igst", filter=Q(status="accepted")), zero, output_field=money),
            won_cgst_sgst=Coalesce(Sum(F("cgst") + F("sgst"), filter=Q(status="accepted")), zero, output_field=money),
        )
        closed = stats["accepted"] + stats["rejected"] + stats["expired"]
        stats["win_rate"] = round(stats["accepted"] * 100 / closed, 2) if closed else 0
        return Response(stats)


class QuoteTaxSettingsView(APIView):
    """Get or update the seller GSTIN / GST state for the current schema's quote owner."""

    def get_permissions(self):
        if connection.schema_name == get_public_schema_name():
            return [IsPlatformAdmin()]
        return [IsAuthenticated()]

    def _get_settings(self, request):
        if connection.schema_name == get_public_schema_name():
            tenant = None
            defaults = {"gstin": "", "state_code": "29", "state_name": "Karnataka"}
        else:
            tenant = request.tenant
            gstin = tenant.gst_number or ""
            code = gst.state_code_from_gstin(gstin)
            defaults = {"gstin": gstin, "state_code": code, "state_name": gst.STATE_CODES.get(code, "")}
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
        for quote in quotes:                       # only drafts: sent quotes keep the tax they were sent with
            recalculate(quote)
        return Response(serializer.data)

class PublicQuoteResponseView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def _allowed_amp_sender(self, sender):
        allowed = {email.strip().lower() for email in settings.QUOTE_AMP_ALLOWED_SENDERS if email.strip()}
        return sender and sender.strip().lower() in allowed

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        sender = request.headers.get("AMP-Email-Sender")
        source_origin = request.query_params.get("__amp_source_origin")
        if sender and self._allowed_amp_sender(sender):
            response["AMP-Email-Allow-Sender"] = sender
        elif source_origin and self._allowed_amp_sender(source_origin):
            origin = request.headers.get("Origin")
            if origin:
                response["Access-Control-Allow-Origin"] = origin
                response["AMP-Access-Control-Allow-Source-Origin"] = source_origin
                response["Access-Control-Expose-Headers"] = "AMP-Access-Control-Allow-Source-Origin"
        return response

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
            "gstin": quote.gstin,
            "place_of_supply": quote.state,
            "supply_type": quote.supply_type,
            "quote_date": quote.quote_date,
            "valid_until": quote.valid_until,
            "lines": QuoteLineSerializer(quote.lines.all(), many=True).data,
            "subtotal": quote.subtotal,
            "discount_total": quote.discount_total,
            "taxable_amount": quote.taxable_amount,
            "cgst": quote.cgst,
            "sgst": quote.sgst,
            "state_tax_label": quote.state_tax_label,
            "igst": quote.igst,
            "freight": quote.freight,
            "packing": quote.packing,
            "round_off": quote.round_off,
            "total": quote.total,
            "notes": quote.notes,
            "terms": services.quote_terms(quote),
        })


    def post(self, request, token):
        amp_sender = request.headers.get("AMP-Email-Sender") or request.query_params.get("__amp_source_origin")
        if amp_sender and not self._allowed_amp_sender(amp_sender):
            raise PermissionDenied("This email sender is not allowed to submit AMP quote responses.")
        accepted = request.data.get("accepted")
        if isinstance(accepted, str) and accepted.lower() in {"true", "false"}:
            accepted = accepted.lower() == "true"
        if not isinstance(accepted, bool):
            raise ValidationError({"accepted": "Must be a boolean."})
        try:
            quote = respond_to_quote(token, accepted)
        except Quote.DoesNotExist:
            return Response({"detail": "Quote not found."}, status=404)
        except ValueError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"number": quote.number, "status": quote.status})


class InvoiceViewSet(viewsets.ModelViewSet):
    filterset_class = InvoiceFilter
    search_fields = ["number", "customer_name", "buyer_email", "gstin", "your_ref", "tenant__name"]
    ordering_fields = ["issue_date", "due_date", "total", "number", "created_at", "status"]
    ordering = ["-issue_date", "-created_at"]

    def get_permissions(self):
        if connection.schema_name == get_public_schema_name():
            return [IsPlatformAdmin()]
        return [IsAuthenticated()]

    def handle_exception(self, exc):
        return super().handle_exception(exc)

    def get_queryset(self):
        qs = Invoice.objects.select_related("tenant")
        if connection.schema_name != get_public_schema_name():
            qs = qs.filter(tenant=self.request.tenant)
        if self.action == "list":
            return qs
        return qs.prefetch_related("items", "payments")

    def get_serializer_class(self):
        return InvoiceListSerializer if self.action == "list" else InvoiceSerializer

    @staticmethod
    def _ensure_draft(invoice):
        if invoice.status != Invoice.Status.DRAFT:
            raise ValidationError(
                "Only draft invoices can be edited or deleted. Void it and create a new one instead."
            )

    def perform_create(self, serializer):
        if connection.schema_name != get_public_schema_name():
            serializer.save(tenant=self.request.tenant)
        else:
            serializer.save()

    def perform_update(self, serializer):
        self._ensure_draft(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._ensure_draft(instance)
        instance.delete()

    @action(detail=False, methods=["get"])
    def defaults(self, request):
        """Prefill data for the 'Raise invoice' form."""
        today = timezone.localdate()
        tenant_id = None if connection.schema_name == get_public_schema_name() else request.tenant.pk
        issuer = services.default_issuer()
        seller_code, seller_name, seller_gstin = services.get_seller_tax_settings(tenant_id)
        if seller_gstin:
            issuer["gstin"] = seller_gstin
        if seller_name:
            issuer["state"] = seller_name
        return Response({
            "number": services.next_invoice_number(today, lock=False),
            "issue_date": today,
            "due_days": services.default_due_days(),
            "issuer": issuer,
            "terms": services.default_terms(),
        })

    @action(detail=False, methods=["get"])
    def meta(self, request):
        """Dropdown options: states, UOMs, GST slabs, line types, payment methods."""
        return Response({
            "states": [{"code": c, "name": n} for c, n in sorted(gst.STATE_CODES.items(), key=lambda kv: kv[1])],
            "gst_rates": [str(r) for r in gst.GST_RATES],
            "line_types": [{"value": v, "label": l} for v, l in InvoiceItem.LineType.choices],
            "payment_methods": [{"value": v, "label": l} for v, l in Payment.Method.choices],
            "statuses": [{"value": v, "label": l} for v, l in Invoice.Status.choices],
        })

    @action(detail=False, methods=["get"], url_path="gstin-info")
    def gstin_info(self, request):
        """Validate a GSTIN offline and say which GST applies against the seller's state."""
        gstin = request.query_params.get("gstin", "").strip().upper()
        if not gst.is_valid_gstin(gstin):
            raise ValidationError({"gstin": "Not a valid GSTIN (format or check digit)."})
        seller_code, seller_name, _ = services.get_seller_tax_settings(None)
        if not seller_code:
            issuer = services.default_issuer()
            seller_code = gst.resolve_state_code(issuer.get("gstin", ""), issuer.get("state", ""))
            seller_name = gst.STATE_CODES.get(seller_code, issuer.get("state", ""))
        code = gst.state_code_from_gstin(gstin)
        stype = gst.supply_type(seller_code, code)
        return Response({
            "gstin": gstin,
            "pan": gst.pan_from_gstin(gstin),
            "state_code": code,
            "state": gst.STATE_CODES[code],
            "supply_type": stype,
            "tax": SUPPLY_LABELS.get(stype, stype),
            "seller_state": seller_name,
        })

    @action(detail=False, methods=["get"], url_path="next-number")
    def next_number(self, request):
        """Preview for the form's Invoice number field. The real number is assigned on raise."""
        return Response({"number": services.next_invoice_number(timezone.localdate(), lock=False)})

    @action(detail=True, methods=["post"], url_path="raise")
    def raise_invoice(self, request, pk=None):
        invoice = services.issue_invoice(self.get_object())
        return Response(InvoiceSerializer(invoice, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        invoice = services.void_invoice(self.get_object())
        return Response(InvoiceSerializer(invoice, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["get"])
    def pdf(self, request, pk=None):
        invoice = self.get_object()
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas
        except ImportError:
            return Response({"detail": "PDF rendering is unavailable; install reportlab."}, status=503)
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        y = 800
        issuer = invoice.issuer or {}
        rows = [
            f"INVOICE {invoice.number or 'DRAFT'}",
            f"Issuer: {issuer.get('company_name', 'O-HM Senior Care')}",
            f"GSTIN: {issuer.get('gstin') or '—'}  State: {issuer.get('state') or '—'}",
            "",
            f"Billed to: {invoice.customer_name}",
            f"GSTIN: {invoice.gstin or '—'}  Place of supply: {invoice.state} ({invoice.state_code or '—'})",
            f"Email: {invoice.buyer_email or '—'}",
            f"Issue date: {invoice.issue_date}  Due date: {invoice.due_date or '—'}",
            f"Status: {invoice.get_status_display()}",
            "",
        ]
        for item in invoice.items.all():
            rows.append(
                f"{item.position}. {item.description} [HSN/SAC {item.hsn_sac}] "
                f"{item.quantity} {item.uom} @ {item.unit_price}  disc {item.discount_percent}%  "
                f"GST {item.gst_rate}%  = {item.amount}"
            )
        rows += [
            "",
            f"Subtotal: {invoice.subtotal}  Discount: {invoice.discount}  Tax: {invoice.tax_amount}",
        ]
        if invoice.is_interstate:
            rows.append(f"IGST: {invoice.igst_amount}")
        else:
            rows.append(f"CGST: {invoice.cgst_amount}  SGST/UTGST: {invoice.sgst_amount}")
        rows += [
            f"Total: {invoice.total} {invoice.currency}",
            f"Amount Paid: {invoice.amount_paid}  Balance Due: {invoice.balance_due}",
        ]
        if invoice.notes:
            rows += ["", f"Notes: {invoice.notes}"]
        if invoice.terms:
            rows += ["", f"Terms: {invoice.terms}"]
        for text in rows:
            if y < 50:
                pdf.showPage()
                y = 800
            pdf.drawString(48, y, str(text)[:110])
            y -= 18
        pdf.save()
        from django.http import HttpResponse
        response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
        filename = f"Invoice-{invoice.number.replace('/', '-') if invoice.number else 'DRAFT'}.pdf"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @action(detail=True, methods=["post"], url_path="record-payment")
    def record_payment(self, request, pk=None):
        invoice = self.get_object()
        ser = RecordPaymentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        payment = services.record_payment(invoice, user=request.user, **ser.validated_data)
        return Response(PaymentReadSerializer(payment).data, status=status.HTTP_201_CREATED)