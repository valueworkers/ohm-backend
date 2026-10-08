# serializers.py
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from .models import Tenant, Domain, TenantProvisioning
from .utils import HOST_RE, SLUG_RE, schema_for_slug


class TenantSerializer(serializers.ModelSerializer):
    domains = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            "id",
            "name",
            "schema_name",
            "status",
            "modules",
            "created_at",
            "domains",
        ]
        read_only_fields = ["id", "schema_name", "created_at", "domains"]

    def get_domains(self, tenant):
        return [
            {"domain": domain.domain, "verified": domain.verified, "is_primary": domain.is_primary}
            for domain in tenant.domains.order_by("-is_primary", "id")
        ]


class ProvisionTenantSerializer(serializers.Serializer):
    """Create a tenant from a root-approved onboarding application."""

    onboarding_request = serializers.PrimaryKeyRelatedField(
        queryset=TenantProvisioning.objects.all(),
        write_only=True,
    )

    def validate_onboarding_request(self, onboarding_request):
        if onboarding_request.status != TenantProvisioning.Status.APPROVED:
            raise serializers.ValidationError("Only an approved onboarding request can create a tenant.")
        return onboarding_request


class RootSessionSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_email(self, value):
        return value.strip().lower()


class OnboardingRequestSerializer(serializers.ModelSerializer):
    """Applicant-facing multi-step onboarding form payload."""

    password = serializers.CharField(write_only=True, trim_whitespace=False, required=False)

    class Meta:
        model = TenantProvisioning
        fields = [
            "id", "contact_name", "contact_email", "contact_phone",
            "organization_name", "organization_type", "organization_address", "slug", "custom_domain",
            "requested_modules", "password", "status", "applicant_message", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "status", "applicant_message", "created_at", "updated_at"]

    def validate_contact_email(self, value):
        return value.strip().lower()

    def validate(self, attrs):
        if self.instance is None and not attrs.get("password"):
            raise serializers.ValidationError({"password": "Choose an initial Tenant Admin password."})
        return attrs

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
        duplicate = TenantProvisioning.objects.filter(
            slug=value,
            status__in=[
                TenantProvisioning.Status.SUBMITTED,
                TenantProvisioning.Status.IN_REVIEW,
                TenantProvisioning.Status.NEEDS_INFO,
                TenantProvisioning.Status.APPROVED,
                TenantProvisioning.Status.PROVISIONING,
            ],
        )
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
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
        pending = TenantProvisioning.objects.filter(
            custom_domain=value,
            status__in=[
                TenantProvisioning.Status.SUBMITTED,
                TenantProvisioning.Status.IN_REVIEW,
                TenantProvisioning.Status.NEEDS_INFO,
                TenantProvisioning.Status.APPROVED,
                TenantProvisioning.Status.PROVISIONING,
            ],
        )
        if self.instance:
            pending = pending.exclude(pk=self.instance.pk)
        if pending.exists():
            raise serializers.ValidationError("There is already an open request for this custom domain.")
        return value

    def validate_requested_modules(self, value):
        return self._validate_string_list(value, "module")

    @staticmethod
    def _validate_string_list(value, label):
        if not isinstance(value, list):
            raise serializers.ValidationError(f"Provide requested {label}s as a list.")
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise serializers.ValidationError(f"Each requested {label} must be a non-empty string.")
        return list(dict.fromkeys(item.strip() for item in value))

    def create(self, validated_data):
        password = validated_data.pop("password")
        validated_data["initial_password_hash"] = make_password(password)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        if password is not None:
            instance.initial_password_hash = make_password(password)
        return super().update(instance, validated_data)


class RootOnboardingRequestSerializer(serializers.ModelSerializer):
    reviewed_by_email = serializers.EmailField(source="reviewed_by.email", read_only=True, allow_null=True)
    tenant_id = serializers.IntegerField(read_only=True, allow_null=True)

    class Meta:
        model = TenantProvisioning
        fields = [
            "id", "contact_name", "contact_email", "contact_phone", "organization_name", "organization_type",
            "organization_address", "slug", "custom_domain", "requested_modules", "approved_modules", "status",
            "review_notes", "applicant_message", "reviewed_by_email", "reviewed_at", "tenant_id",
            "provision_error", "created_at", "updated_at",
        ]
        read_only_fields = fields


class RootOnboardingRequestUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[
        TenantProvisioning.Status.IN_REVIEW,
        TenantProvisioning.Status.NEEDS_INFO,
        TenantProvisioning.Status.APPROVED,
        TenantProvisioning.Status.REJECTED,
    ])
    approved_modules = serializers.ListField(child=serializers.CharField(max_length=80), required=False, default=list)
    review_notes = serializers.CharField(required=False, allow_blank=True, default="")
    applicant_message = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_approved_modules(self, value):
        if any(not item.strip() for item in value):
            raise ValidationError("Selected modules cannot be blank.")
        return list(dict.fromkeys(item.strip() for item in value))

    def validate(self, attrs):
        request = self.instance
        status = attrs["status"]
        if status == TenantProvisioning.Status.NEEDS_INFO and not attrs.get("applicant_message", "").strip():
            raise serializers.ValidationError({"applicant_message": "This field is required for needs_info."})
        if status == TenantProvisioning.Status.APPROVED:
            requested = set(request.requested_modules)
            if not set(attrs["approved_modules"]).issubset(requested):
                raise serializers.ValidationError({
                    "approved_modules": "Select only modules requested by the applicant."
                })
        return attrs
