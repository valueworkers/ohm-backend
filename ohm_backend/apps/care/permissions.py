from django.db import connection
from django_tenants.utils import get_public_schema_name
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from apps.accounts.permissions import (
    DynamicModelPermissions,
    is_root_user,
    is_superadmin,
    is_tenant_admin,
)


class HasProductAccess(BasePermission):
    """
    Plan gating permission.
    Verifies that the tenant resolving the request has the product feature
    subscribed in its Plan or modules list.
    """
    message = "Your workspace subscription plan does not include access to the Care product."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False

        # Superadmins and root platform operators have administrative bypass
        if is_superadmin(request.user) or is_root_user(request.user):
            return True

        tenant = getattr(request, "tenant", None)
        if not tenant:
            return False

        # In public schema, tenant product endpoints should not be accessible
        if connection.schema_name == get_public_schema_name():
            return False

        product_code = getattr(view, "product_code", "care")
        if not product_code:
            return True

        if hasattr(tenant, "has_feature"):
            if tenant.has_feature(product_code) or tenant.has_feature("care") or tenant.has_feature("medicare"):
                return True

        raise PermissionDenied(self.message)


class CareModelPermissions(DynamicModelPermissions):
    """
    Model permissions enforcing view permission on GET,
    add on POST, change on PUT/PATCH, delete on DELETE.
    """
    perms_map = {
        "GET": ["%(app_label)s.view_%(model_name)s"],
        "OPTIONS": [],
        "HEAD": ["%(app_label)s.view_%(model_name)s"],
        "POST": ["%(app_label)s.add_%(model_name)s"],
        "PUT": ["%(app_label)s.change_%(model_name)s"],
        "PATCH": ["%(app_label)s.change_%(model_name)s"],
        "DELETE": ["%(app_label)s.delete_%(model_name)s"],
    }


# Backwards-compatible alias
MedicareModelPermissions = CareModelPermissions
