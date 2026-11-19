"""
apps/platform/forms.py
──────────────────────
Custom authentication forms for multi-tenant Django administration.
"""
from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class TenantAdminAuthenticationForm(AdminAuthenticationForm):
    """Admin login form with optional X-Tenant workspace slug.

    Leaving X-Tenant blank authenticates against the public schema (Platform/Root Admin).
    Providing an X-Tenant slug switches the context to that tenant schema.
    """

    x_tenant = forms.CharField(
        label=_("Tenant Slug"),
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": "Workspace slug (leave empty for Platform Admin)",
                "autocapitalize": "none",
                "autocomplete": "off",
                "spellcheck": "false",
            }
        ),
        help_text=_(
            "Enter your tenant workspace slug (e.g. acme-clinic), or leave empty for Platform / Root Admin."
        ),
    )

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(request=request, *args, **kwargs)
        # Pre-populate x_tenant field from query param or cookie if not bound
        if request and not self.is_bound:
            initial_slug = (
                request.GET.get("x-tenant")
                or request.GET.get("x_tenant")
                or request.COOKIES.get("x_tenant")
            )
            if initial_slug:
                self.fields["x_tenant"].initial = initial_slug.strip()

    def clean(self):
        # If the middleware detected an invalid, missing, or inactive tenant:
        admin_tenant_error = getattr(self.request, "_admin_tenant_error", None)
        if admin_tenant_error:
            self.add_error("x_tenant", admin_tenant_error)
            raise ValidationError(admin_tenant_error)

        return super().clean()

    def confirm_login_allowed(self, user):
        if not user.is_active:
            raise ValidationError(
                _("This account is inactive."),
                code="inactive",
            )
        if not user.is_staff:
            raise ValidationError(
                _("This account does not have staff permissions to access the admin panel."),
                code="no_staff",
            )
