from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from .models import Tenant, Domain, TenantProvisioning, Feature
from .utils import HOST_RE, SLUG_RE, schema_for_slug

S = TenantProvisioning.Status
OPEN_STATUSES = [S.SUBMITTED, S.APPROVED, S.PROVISIONING]

class HomeFeatureSerializer(serializers.ModelSerializer):
    class Meta:
        model = Feature
        fields = ["code", "name", "description"]
        read_only_fields = fields

class TenantSerializer(serializers.ModelSerializer):
    domains = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = ["id", "name", "schema_name", "status", "modules", "created_at", "domains"]
        read_only_fields = fields

    def get_domains(self, tenant):
        return [
            {"domain": d.domain, "verified": d.verified, "is_primary": d.is_primary}
            for d in tenant.domains.order_by("-is_primary", "id")
        ]


class TenantRequestCreateSerializer(serializers.ModelSerializer):
    """Public signup: creates the applicant's public user and the request together."""

    full_name = serializers.CharField(write_only=True, max_length=200)
    email = serializers.EmailField(write_only=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    applicant_name = serializers.CharField(source="applicant.full_name", read_only=True)
    applicant_email = serializers.EmailField(source="applicant.email", read_only=True)

    class Meta:
        model = TenantProvisioning
        fields = [
            "id", "full_name", "email", "password", "applicant_name", "applicant_email",
            "contact_phone", "organization_name", "organization_type", "organization_address",
            "slug", "custom_domain", "requested_modules", "status", "created_at",
        ]
        read_only_fields = ["id", "status", "created_at"]

    def validate_email(self, value):
        value = value.strip().lower()
        if get_user_model().objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def validate_slug(self, value):
        value = value.strip().lower()
        if not SLUG_RE.fullmatch(value):
            raise serializers.ValidationError("Use 3-30 lowercase letters, digits, or hyphens; begin with a letter.")
        if value in settings.RESERVED_SLUGS:
            raise serializers.ValidationError("This workspace name is reserved.")
        if Tenant.objects.filter(schema_name=schema_for_slug(value)).exists():
            raise serializers.ValidationError("This workspace name is already in use.")
        if TenantProvisioning.objects.filter(slug=value, status__in=OPEN_STATUSES).exists():
            raise serializers.ValidationError("There is already an open request for this workspace name.")
        return value

    def validate_custom_domain(self, value):
        value = value.strip().lower()
        if not value:
            return ""
        if not HOST_RE.fullmatch(value):
            raise serializers.ValidationError("Enter a valid hostname, e.g. app.yourclinic.com.")
        if value == settings.BASE_DOMAIN or value.endswith("." + settings.BASE_DOMAIN) or value in settings.ROOT_HOSTS:
            raise serializers.ValidationError("Use a domain you own, not an OHM platform domain.")
        if Domain.objects.filter(domain=value).exists():
            raise serializers.ValidationError("This domain is already connected to a tenant.")
        if TenantProvisioning.objects.filter(custom_domain=value, status__in=OPEN_STATUSES).exists():
            raise serializers.ValidationError("There is already an open request for this custom domain.")
        return value

    def validate_requested_modules(self, value):
        if not isinstance(value, list) or any(not isinstance(m, str) or not m.strip() for m in value):
            raise serializers.ValidationError("Provide requested modules as a list of non-empty strings.")
        modules = list(dict.fromkeys(m.strip() for m in value))
        known = set(Feature.objects.filter(is_active=True, code__in=modules).values_list("code", flat=True))
        unknown = [m for m in modules if m not in known]
        if unknown:
            raise serializers.ValidationError(f"Unknown modules: {', '.join(unknown)}.")
        return modules

    @transaction.atomic
    def create(self, validated_data):
        applicant = get_user_model().objects.create_user(
            email=validated_data.pop("email"),
            password=validated_data.pop("password"),
            full_name=validated_data.pop("full_name"),
        )
        return super().create({**validated_data, "applicant": applicant})


class TenantRequestSerializer(serializers.ModelSerializer):
    """Read-only view for the applicant (their own requests)."""

    applicant_name = serializers.CharField(source="applicant.full_name", read_only=True)
    applicant_email = serializers.EmailField(source="applicant.email", read_only=True)

    class Meta:
        model = TenantProvisioning
        fields = [
            "id", "applicant_name", "applicant_email", "contact_phone", "organization_name",
            "organization_type", "organization_address", "slug", "custom_domain",
            "requested_modules", "approved_modules", "status", "review_notes", "created_at",
        ]
        read_only_fields = fields


class RootTenantRequestSerializer(TenantRequestSerializer):
    reviewed_by_email = serializers.EmailField(source="reviewed_by.email", read_only=True, allow_null=True)
    tenant_id = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta(TenantRequestSerializer.Meta):
        fields = TenantRequestSerializer.Meta.fields + [
            "reviewed_by_email", "reviewed_at", "tenant_id", "provision_error", "updated_at",
        ]
        read_only_fields = fields


class TenantRequestApprovalSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[S.APPROVED, S.REJECTED])
    # Omitted fields stay untouched.
    approved_modules = serializers.ListField(child=serializers.CharField(max_length=80), required=False)
    review_notes = serializers.CharField(required=False, allow_blank=True)

    def validate_approved_modules(self, value):
        if any(not m.strip() for m in value):
            raise serializers.ValidationError("Selected modules cannot be blank.")
        return list(dict.fromkeys(m.strip() for m in value))

    def validate(self, attrs):
        modules = attrs.get("approved_modules")
        if attrs["status"] == S.APPROVED and modules is not None:
            if not set(modules).issubset(self.instance.requested_modules):
                raise serializers.ValidationError(
                    {"approved_modules": "Select only modules requested by the applicant."}
                )
        return attrs