import re
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction, connection
from django.db.models import Q
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from rest_framework import serializers

from .models import (
    Domain,
    Feature,
    Plan,
    Subscription,
    Tenant,
    TenantProvisioning,
    Quote, QuoteEvent, QuoteLine, QuoteTaxSettings,
)
from apps.accounts.services import AccountsService
from .utils import HOST_RE, SLUG_RE, base_domain, slug_problem

DEFAULT_TRIAL_PLAN_ID = 1
DEFAULT_TRIAL_DAYS = 14


def default_trial_plan():
    plan = Plan.objects.filter(pk=DEFAULT_TRIAL_PLAN_ID, is_active=True).first()
    if plan is None:
        raise serializers.ValidationError(
            f"Default trial plan (id={DEFAULT_TRIAL_PLAN_ID}) is missing or inactive."
        )
    return plan


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------

class FeatureSerializer(serializers.ModelSerializer):
    class Meta:
        model = Feature
        fields = ("code", "name", "description")


class PlanSerializer(serializers.ModelSerializer):
    features = FeatureSerializer(many=True, read_only=True)

    class Meta:
        model = Plan
        fields = ("id", "code", "name", "description", "features", "billing_period", "currency", "amount", "setup_fee", "tax_rate", "max_employees", "max_branches")

class PlanAdminSerializer(serializers.ModelSerializer):
    """Used by ohm admin for reads and writes."""
    features = FeatureSerializer(many=True, read_only=True)
    feature_ids = serializers.PrimaryKeyRelatedField(
        queryset=Feature.objects.all(),
        many=True,
        write_only=True,
        required=False,
        source="features",
    )

    class Meta:
        model = Plan
        fields = (
            "id", "code", "name", "description",
            "features", "feature_ids",
            "billing_period", "currency", "amount", "setup_fee", "tax_rate",
            "max_employees", "max_branches",
            "is_active", "created_at",
        )
        read_only_fields = ("id", "created_at")


# --------------------------------------------------------------------------
# Onboarding
# --------------------------------------------------------------------------

class BranchSerializer(serializers.Serializer):
    name= serializers.CharField(max_length=100)
    line1 = serializers.CharField(max_length=255)
    line2 = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    locality = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    city = serializers.CharField(max_length=100)
    pincode = serializers.RegexField(
        r"^[1-9]\d{5}$", error_messages={"invalid": "Enter a valid 6-digit pincode."}
    )


class TenantProvisioningCreateSerializer(serializers.ModelSerializer):
    branches = serializers.JSONField(required=False)
    requested_plan = serializers.PrimaryKeyRelatedField(
        queryset=Plan.objects.filter(is_active=True), required=False, default=default_trial_plan
    )

    class Meta:
        model = TenantProvisioning
        fields = (
            "id", "organization_name", "logo", "email", "contact_phone", "gst_number",
            "branches", "ops_admin_email", "co_owner_emails", "slug", "custom_domain",
            "wants_custom_website", "requested_plan", "status", "created_at",
        )
        read_only_fields = ("id", "status", "created_at")

    def validate_branches(self, value):
        serializer = BranchSerializer(data=value or [], many=True)
        serializer.is_valid(raise_exception=True)
        return [dict(item) for item in serializer.validated_data]


    def validate_slug(self, value):
        value = value.strip().lower()
        # exclude self so editing a request doesn't clash with its own slug
        problem = slug_problem(value, exclude_pk=self.instance.pk if self.instance else None)
        if problem:
            raise serializers.ValidationError(problem)
        return value

    def validate_custom_domain(self, value):
        value = value.strip().lower()
        if not value:
            return ""
        if not HOST_RE.match(value):
            raise serializers.ValidationError("Enter a valid domain, e.g. portal.example.com.")
        base = base_domain()
        if value == base or value.endswith(f".{base}"):
            raise serializers.ValidationError(f"Use the subdomain field for hosts under {base}.")
        if Domain.objects.filter(domain=value).exists():
            raise serializers.ValidationError("This domain is already in use.")
        open_requests = TenantProvisioning.objects.filter(
            custom_domain=value, status__in=TenantProvisioning.OPEN_STATUSES
        )
        if self.instance:
            open_requests = open_requests.exclude(pk=self.instance.pk)
        if open_requests.exists():
            raise serializers.ValidationError("This domain is already requested.")
        return value

    def _get_or_create_applicant(self, email):
        User = get_user_model()
        user = User.objects.filter(email__iexact=email).first()
        if user:
            return user  # reuse; never modify an existing account from a public request
        try:
            with transaction.atomic():  # savepoint, so a race doesn't poison the outer transaction
                user = User(email=email, is_active=False)
                user.set_unusable_password()
                user.save()
        except IntegrityError:
            user = User.objects.get(email__iexact=email)  # someone created it a moment ago
        return user

    @transaction.atomic
    def create(self, validated_data):
        validated_data["applicant"] = self._get_or_create_applicant(validated_data["email"])
        return super().create(validated_data)

class TenantProvisioningUpdateSerializer(TenantProvisioningCreateSerializer):
    @transaction.atomic
    def update(self, instance, validated_data):
        # not a validation: keeps the applicant (tenant owner) in sync with an email change
        email = validated_data.get("email")
        if email and email != instance.applicant.email.lower():
            validated_data["applicant"] = self._get_or_create_applicant(email)
        return super().update(instance, validated_data)


class TenantProvisioningSerializer(serializers.ModelSerializer):
    """Read view. provision_error is only shown to platform admins."""

    applicant_email = serializers.EmailField(source="applicant.email", read_only=True)
    requested_plan_name = serializers.CharField(source="requested_plan.name", read_only=True, default=None)
    tenant_schema = serializers.CharField(source="tenant.schema_name", read_only=True, default=None)

    class Meta:
        model = TenantProvisioning
        fields = (
            "id", "applicant_email", "organization_name", "logo", "email", "contact_phone",
            "gst_number", "branches", "ops_admin_email", "co_owner_emails", "slug",
            "custom_domain", "wants_custom_website", "requested_plan", "requested_plan_name",
            "status", "review_notes", "reviewed_at", "tenant_schema",
            "provision_error", "created_at", "updated_at",
        )
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        is_admin = bool(user and (user.is_superuser or hasattr(user, "platform_admin_profile")))
        if not is_admin:
            data.pop("provision_error", None)
        return data


class ReviewSerializer(serializers.Serializer):
    review_notes = serializers.CharField(required=False, allow_blank=True)


class ApproveSerializer(serializers.Serializer):
    review_notes = serializers.CharField(required=False, allow_blank=True)
    requested_plan = serializers.PrimaryKeyRelatedField(
        queryset=Plan.objects.filter(is_active=True), required=False
    )


class RejectSerializer(serializers.Serializer):
    review_notes = serializers.CharField(allow_blank=False)


# --------------------------------------------------------------------------
# Tenants and subscriptions (platform admin)
# --------------------------------------------------------------------------

class DomainSerializer(serializers.ModelSerializer):
    class Meta:
        model = Domain
        fields = ("id", "domain", "is_primary", "verified", "verified_at")
        read_only_fields = fields


class SubscriptionSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source="plan.name", read_only=True, default=None)
    price_details = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = (
            "id", "tenant", "plan", "plan_name", "price_details", "status", "starts_at", "ends_at",
            "trial_ends_at", "billing_email", "features", "created_at",
        )
        read_only_fields = ("id", "created_at")

    def validate(self, attrs):
        starts = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        ends = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
        if starts and ends and ends <= starts:
            raise serializers.ValidationError({"ends_at": "Must be after the start date."})
        return attrs

    def create(self, validated_data):
        plan = validated_data.get("plan")
        if plan:
            validated_data.update(
                price_billing_period=plan.billing_period,
                price_currency=plan.currency,
                price_amount=plan.amount,
                price_setup_fee=plan.setup_fee,
                price_tax_rate=plan.tax_rate,
            )
        return super().create(validated_data)

    def update(self, instance, validated_data):
        plan = validated_data.get("plan")
        if plan and plan.pk != instance.plan_id:
            validated_data.update(
                price_billing_period=plan.billing_period,
                price_currency=plan.currency,
                price_amount=plan.amount,
                price_setup_fee=plan.setup_fee,
                price_tax_rate=plan.tax_rate,
            )
        return super().update(instance, validated_data)

    def get_price_details(self, obj):
        if obj.price_amount is None:
            return None
        return {
            "billing_period": obj.price_billing_period,
            "currency": obj.price_currency,
            "amount": obj.price_amount,
            "setup_fee": obj.price_setup_fee,
            "tax_rate": obj.price_tax_rate,
        }



class TenantCreateSerializer(serializers.ModelSerializer):
    plan = serializers.PrimaryKeyRelatedField(
        queryset=Plan.objects.filter(is_active=True), write_only=True,
        required=False, default=default_trial_plan,
    )
    domain = serializers.CharField(write_only=True, required=False, allow_blank=False)
    owner_ids = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects.all(),
        many=True,
        write_only=True,
        required=False,
        source="owners",
    )

    class Meta:
        model = Tenant
        fields = (
            "id", "name", "schema_name", "owner_ids", "plan", "logo", "contact_email",
            "contact_phone", "timezone","tags", "default_currency", "domain",
        )
        read_only_fields = ("id",)

    def validate_schema_name(self, value):
        value = value.lower().strip()
        if not SLUG_RE.match(value):
            raise serializers.ValidationError(
                "Use lowercase letters, digits and underscores only; must start with a letter."
            )
        if value in settings.RESERVED_SLUGS or value.startswith("pg_"):
            raise serializers.ValidationError("This schema name is reserved.")
        if Tenant.objects.filter(schema_name=value).exists():
            raise serializers.ValidationError("A tenant with this schema name already exists.")
        return value

    def validate_domain(self, value):
        value = value.lower().strip()
        if Domain.objects.filter(domain=value).exists():
            raise serializers.ValidationError("This domain is already in use.")
        return value

    @transaction.atomic
    def create(self, validated_data):
        domain = validated_data.pop("domain", None)
        owners = validated_data.pop("owners", [])
        plan = validated_data.pop("plan")
        now = timezone.now()
        validated_data["modules"] = list(
            plan.features.filter(is_active=True).values_list("code", flat=True)
        )
        # New tenants start as ACTIVE here because a platform admin is creating them directly.
        # Change this if direct creation should go through a different initial status.
        validated_data.setdefault("status", Tenant.Status.ACTIVE)
        tenant = Tenant.objects.create(**validated_data)
        Subscription.objects.create(
            tenant=tenant,
            plan=plan,
            price_billing_period=plan.billing_period,
            price_currency=plan.currency,
            price_amount=plan.amount,
            price_setup_fee=plan.setup_fee,
            price_tax_rate=plan.tax_rate,
            status=(Subscription.Status.TRIAL if plan.pk == DEFAULT_TRIAL_PLAN_ID else Subscription.Status.ACTIVE),
            starts_at=now,
            trial_ends_at=(now + timedelta(days=DEFAULT_TRIAL_DAYS) if plan.pk == DEFAULT_TRIAL_PLAN_ID else None),
            billing_email=tenant.contact_email,
        )
        if owners:
            tenant.owners.set(owners)
            with schema_context(tenant.schema_name):
                for owner in owners:
                    AccountsService.ensure_tenant_admin(owner)
        if domain:
            Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
        return tenant

class TenantUpdateSerializer(serializers.ModelSerializer):
    owner_ids = serializers.PrimaryKeyRelatedField(
        queryset=get_user_model().objects.all(),
        many=True,
        write_only=True,
        required=False,
        source="owners",
    )

    class Meta:
        model = Tenant
        fields = (
            "name", "owner_ids", "modules", "logo", "contact_email",
            "contact_phone", "timezone", "tags", "default_currency",
        )

    @transaction.atomic
    def update(self, instance, validated_data):
        return super().update(instance, validated_data)

class OwnerBriefSerializer(serializers.Serializer):
    """Lightweight representation of a tenant owner for read views."""
    id = serializers.IntegerField(read_only=True)
    email = serializers.EmailField(read_only=True)
    full_name = serializers.CharField(read_only=True)

class TenantProfileSerializer(serializers.ModelSerializer):
    """What a tenant owner may see and edit about their own workspace."""
    subscriptions = SubscriptionSerializer(many=True, read_only=True)
    domains = DomainSerializer(many=True, read_only=True)
    owners = OwnerBriefSerializer(many=True, read_only=True)

    class Meta:
        model = Tenant
        fields = (
            "id", "name", "schema_name", "status", "logo", "contact_email", "contact_phone",
            "timezone", "tags", "default_currency", "modules", "domains", "subscriptions",
            "owners",
        )
        read_only_fields = ("id", "schema_name", "status", "modules", "domains", "owners")

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("This field may not be blank.")
        return value

    def validate_timezone(self, value):
        try:
            ZoneInfo(value)
        except Exception:
            raise serializers.ValidationError("Enter a valid IANA timezone, e.g. Asia/Kolkata.")
        return value

    def validate_default_currency(self, value):
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise serializers.ValidationError("Enter a 3-letter currency code, e.g. INR.")
        return value


# Prospect quote serializers
class QuoteLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuoteLine
        fields = ("id", "position", "kind", "plan", "item", "specification", "hsn_sac", "qty", "unit_price", "lead_time", "gst_rate")
        read_only_fields = ("id", "position")

    def validate(self, attrs):
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        plan = attrs.get("plan", getattr(self.instance, "plan", None))
        if kind == QuoteLine.Kind.CATALOG and plan is None:
            raise serializers.ValidationError({"plan": "Catalog lines must reference a Plan."})
        if kind != QuoteLine.Kind.CATALOG and plan is not None:
            raise serializers.ValidationError({"plan": "Only catalog lines can reference a Plan."})
        if kind == QuoteLine.Kind.ON_REQUEST:
            attrs["unit_price"] = None
        elif attrs.get("unit_price", getattr(self.instance, "unit_price", None)) is None:
            raise serializers.ValidationError({"unit_price": "A price is required for priced lines."})
        return attrs


class QuoteEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuoteEvent
        fields = ("kind", "detail", "actor", "at")


class QuoteSerializer(serializers.ModelSerializer):
    lines = QuoteLineSerializer(many=True)
    events = QuoteEventSerializer(many=True, read_only=True)
    valid_until = serializers.DateField(read_only=True)
    class Meta:
        model = Quote
        fields = ("id", "number", "revision", "status", "company", "attention", "address", "gstin", "state", "buyer_email", "ship_to", "your_ref", "quote_date", "valid_days", "valid_until", "notes", "subtotal", "cgst", "sgst", "igst", "freight", "total", "sent_at", "accepted_at", "created_at", "lines", "events")
        read_only_fields = ("id", "number", "revision", "status", "quote_date", "subtotal", "cgst", "sgst", "igst", "total", "sent_at", "accepted_at", "created_at")

    def validate_lines(self, lines):
        if not lines:
            raise serializers.ValidationError("At least one line is required.")
        return lines

    def create(self, validated_data):
        from .services import create_quote

        request = self.context["request"]
        lines = validated_data.pop("lines")
        tenant = None if connection.schema_name == get_public_schema_name() else request.tenant
        return create_quote(request.user, validated_data, lines, tenant=tenant)

    def update(self, instance, validated_data):
        lines = validated_data.pop("lines", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        instance.save()
        if lines is not None:
            instance.lines.all().delete()
            for index, line in enumerate(lines, 1):
                QuoteLine.objects.create(quote=instance, position=index, **line)
        from .services import recalculate

        recalculate(instance)
        QuoteEvent.objects.create(quote=instance, kind="updated", actor=self.context["request"].user if instance.tenant_id is None else None, detail="" if instance.tenant_id is None else self.context["request"].user.email)
        return instance


class SendTestSerializer(serializers.Serializer):
    recipients = serializers.ListField(child=serializers.EmailField(), allow_empty=False)

class QuoteTaxSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = QuoteTaxSettings
        fields = ("state_code", "state_name", "updated_at")
        read_only_fields = ("updated_at",)

    def validate(self, attrs):
        state_code = attrs.get("state_code", getattr(self.instance, "state_code", ""))
        state_name = attrs.get("state_name", getattr(self.instance, "state_name", ""))
        if state_code and (len(state_code) != 2 or not state_code.isdigit()):
            raise serializers.ValidationError({"state_code": "Enter a two-digit GST state code."})
        if not state_code and not state_name:
            raise serializers.ValidationError("Set a state code or state name.")
        return attrs
