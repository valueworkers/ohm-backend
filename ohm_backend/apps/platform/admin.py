from django.contrib import admin, messages
from django.utils import timezone
from django_tenants.utils import get_public_schema_name

from .models import Tenant, Domain, Feature, PlatformAdmin, Subscription, TenantProvisioning
from .services import review_onboarding_request


class DomainInline(admin.TabularInline):
    model = Domain
    extra = 0
    readonly_fields = ("verification_token", "verified_at")


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    """Super Admin console for tenants; provision them through approved tenant requests."""

    list_display = ("name", "schema_name", "status", "modules", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "schema_name")
    inlines = [DomainInline]
    actions = ["suspend", "activate"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False  # dropping a tenant's data must never be one click

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.schema_name == get_public_schema_name():
            return ("schema_name", "name", "status", "modules", "created_at")  # protect the platform itself
        return ("schema_name", "created_at")

    @admin.action(description="Suspend selected workspaces")
    def suspend(self, request, queryset):
        n = queryset.exclude(schema_name=get_public_schema_name()).update(status=Tenant.Status.SUSPENDED)
        self.message_user(request, f"{n} workspace(s) suspended.")

    @admin.action(description="Activate selected workspaces")
    def activate(self, request, queryset):
        n = queryset.exclude(schema_name=get_public_schema_name()).update(status=Tenant.Status.ACTIVE)
        self.message_user(request, f"{n} workspace(s) activated.")


@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ("domain", "tenant", "is_primary", "verified", "verified_at")
    list_filter = ("verified",)
    search_fields = ("domain", "tenant__name")
    readonly_fields = ("verification_token", "verified_at", "created_at")
    actions = ["mark_verified"]

    @admin.action(description="Mark selected domains as verified")
    def mark_verified(self, request, queryset):
        n = queryset.filter(verified=False).update(verified=True, verified_at=timezone.now())
        self.message_user(request, f"{n} domain(s) verified.")


@admin.register(Feature)
class FeatureAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("tenant", "status", "starts_at", "ends_at")
    list_filter = ("status",)
    filter_horizontal = ("features",)


@admin.register(PlatformAdmin)
class PlatformAdminProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "created_at")
    search_fields = ("user__email", "user__full_name")


@admin.register(TenantProvisioning)
class TenantProvisioningAdmin(admin.ModelAdmin):
    """The lobby: root reviews tenant requests here. Approval creates the tenant."""

    list_display = (
        "organization_name", "applicant_email", "slug", "status",
        "created_at", "reviewed_by", "reviewed_at",
    )
    list_filter = ("status", "created_at")
    search_fields = (
        "organization_name", "slug", "custom_domain",
        "applicant__email", "applicant__full_name",
    )
    list_select_related = ("applicant", "reviewed_by", "tenant")
    raw_id_fields = ("applicant", "reviewed_by", "tenant")
    readonly_fields = (
        "applicant", "status", "approved_modules", "reviewed_by", "reviewed_at",
        "tenant", "provision_error", "created_at", "updated_at",
    )
    actions = ["approve_selected", "reject_selected"]

    @admin.display(description="Applicant email", ordering="applicant__email")
    def applicant_email(self, obj):
        return obj.applicant.email

    def has_add_permission(self, request):
        # Requests come only from POST /api/tenant-requests/.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _review(self, request, queryset, status):
        done = 0
        for req in queryset:
            try:
                review_onboarding_request(req.pk, status=status, user=request.user)
                done += 1
            except ValueError as exc:
                self.message_user(request, f"{req.organization_name}: {exc}", messages.ERROR)
        if done:
            self.message_user(request, f"{done} request(s) {status}.", messages.SUCCESS)

    @admin.action(description="Approve selected requests (queues provisioning)")
    def approve_selected(self, request, queryset):
        self._review(request, queryset, TenantProvisioning.Status.APPROVED)

    @admin.action(description="Reject selected requests")
    def reject_selected(self, request, queryset):
        self._review(request, queryset, TenantProvisioning.Status.REJECTED)