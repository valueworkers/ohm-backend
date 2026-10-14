import re
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from rest_framework import serializers

from .models import (
    Domain,
    Feature,
    Plan,
    Subscription,
    Tenant,
    TenantProvisioning,
)
from .utils import HOST_RE, SLUG_RE, base_domain, slug_problem
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
        fields = ("id", "code", "name", "description", "features", "max_employees", "max_branches")

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
            "max_employees", "max_branches",
            "is_active", "created_at",
        )
        read_only_fields = ("id", "created_at")

# --------------------------------------------------------------------------
# Onboarding
# --------------------------------------------------------------------------

class BranchSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    address = serializers.CharField(max_length=500)


class TenantProvisioningCreateSerializer(serializers.ModelSerializer):
    # JSONField (not a nested serializer) so branches also works in multipart
    # requests, where the client sends it as a JSON string next to the logo.
    branches = serializers.JSONField(required=False)

    class Meta:
        model = TenantProvisioning
        fields = (
            "id", "organization_name", "logo", "email", "contact_phone", "address",
            "branches", "ops_admin_email", "slug", "custom_domain",
            "wants_custom_website", "requested_plan", "status", "created_at",
        )
        read_only_fields = ("id", "status", "created_at")

    def validate_organization_name(self, value):
        return value.strip()

    def validate_branches(self, value):
        serializer = BranchSerializer(data=value or [], many=True)
        serializer.is_valid(raise_exception=True)
        return [dict(item) for item in serializer.validated_data]

    def validate_slug(self, value):
        value = value.strip().lower()
        problem = slug_problem(value)
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
        if TenantProvisioning.objects.filter(
            custom_domain=value, status__in=TenantProvisioning.OPEN_STATUSES
        ).exists():
            raise serializers.ValidationError("This domain is already requested.")
        return value

    def validate_requested_plan(self, value):
        if value is not None and not value.is_active:
            raise serializers.ValidationError("This plan is not available.")
        return value
    
    def validate_email(self, value):
        return value.strip().lower()

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
    class Meta(TenantProvisioningCreateSerializer.Meta):
        read_only_fields = ("status", "applicant", "tenant", "reviewed_by",
                            "reviewed_at", "review_notes", "provision_error")

class TenantProvisioningSerializer(serializers.ModelSerializer):
    """Read view. provision_error is only shown to platform admins."""

    applicant_email = serializers.EmailField(source="applicant.email", read_only=True)
    requested_plan_name = serializers.CharField(source="requested_plan.name", read_only=True, default=None)
    tenant_schema = serializers.CharField(source="tenant.schema_name", read_only=True, default=None)

    class Meta:
        model = TenantProvisioning
        fields = (
            "id", "applicant_email", "organization_name", "logo", "email", "contact_phone",
            "address", "branches", "ops_admin_email", "slug", "custom_domain",
            "wants_custom_website", "requested_plan", "requested_plan_name",
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

    class Meta:
        model = Subscription
        fields = (
            "id", "tenant", "plan", "plan_name", "status", "starts_at", "ends_at",
            "trial_ends_at", "billing_email", "features", "created_at",
        )
        read_only_fields = ("id", "created_at")

    def validate(self, attrs):
        starts = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        ends = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
        if starts and ends and ends <= starts:
            raise serializers.ValidationError({"ends_at": "Must be after the start date."})
        return attrs



class TenantCreateSerializer(serializers.ModelSerializer):
    domain = serializers.CharField(write_only=True, required=False, allow_blank=False)

    class Meta:
        model = Tenant
        fields = (
            "id", "name", "schema_name", "owner", "modules", "logo", "contact_email",
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
        # New tenants start as ACTIVE here because a platform admin is creating them directly.
        # Change this if direct creation should go through a different initial status.
        validated_data.setdefault("status", Tenant.Status.ACTIVE)
        tenant = Tenant.objects.create(**validated_data)
        if domain:
            Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
        return tenant

class TenantUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = (
            "name", "owner", "modules", "logo", "contact_email",
            "contact_phone", "timezone", "tags", "default_currency",
        )

class TenantProfileSerializer(serializers.ModelSerializer):
    """What a tenant owner may see and edit about their own workspace."""
    subscriptions = SubscriptionSerializer(many=True, read_only=True)
    domains = DomainSerializer(many=True, read_only=True)

    class Meta:
        model = Tenant
        fields = (
            "id", "name", "schema_name", "status", "logo", "contact_email", "contact_phone",
            "timezone", "tags", "default_currency", "modules", "domains", "subscriptions"
        )
        read_only_fields = ("id", "schema_name", "status", "modules", "domains")

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