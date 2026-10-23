"""Tenant-scoped access management built on Django's Group and Permission models."""

from django.contrib.auth.models import Group, Permission
from django_tenants.utils import schema_context

from .models import User


class TenantAccessManager:
    """Manage a tenant's users, groups, and Django permissions in its schema.

    Permission strings use Django's ``app_label.codename`` format, such as
    ``accounts.view_user``. Role names are group names; this class does not
    maintain a separate permission map in Python.
    """

    def __init__(self, tenant_or_schema):
        schema_name = getattr(tenant_or_schema, "schema_name", tenant_or_schema)
        if not schema_name:
            raise ValueError("schema_name is required")
        self.schema_name = schema_name

    def ensure_group(self, group_name):
        with schema_context(self.schema_name):
            group, _ = Group.objects.get_or_create(name=group_name)
            return group

    def assign_group(self, user, group_name):
        """Add a user to a group in this tenant, resolving by email across schemas."""
        email = getattr(user, "email", user)
        with schema_context(self.schema_name):
            tenant_user = User.objects.get(email__iexact=email)
            group, _ = Group.objects.get_or_create(name=group_name)
            tenant_user.groups.add(group)
            return tenant_user

    def remove_group(self, user, group_name):
        email = getattr(user, "email", user)
        with schema_context(self.schema_name):
            tenant_user = User.objects.get(email__iexact=email)
            group = Group.objects.filter(name=group_name).first()
            if group:
                tenant_user.groups.remove(group)
            return tenant_user

    def set_group_permissions(self, group_name, permissions):
        """Replace a group's permissions with Django permission codenames.

        Each item must be a ``Permission`` instance or an ``app_label.codename``
        string. Unknown permission strings raise ``ValueError``.
        """
        with schema_context(self.schema_name):
            group, _ = Group.objects.get_or_create(name=group_name)
            permission_ids = set()
            permission_codes = set()
            for permission in permissions:
                if isinstance(permission, Permission):
                    permission_codes.add((permission.content_type.app_label, permission.codename))
                elif isinstance(permission, str) and "." in permission:
                    app_label, codename = permission.split(".", 1)
                    permission_codes.add((app_label, codename))
                else:
                    raise ValueError("Permissions must be Permission objects or app_label.codename strings")

            for app_label, codename in permission_codes:
                try:
                    permission_ids.add(
                        Permission.objects.get(
                            content_type__app_label=app_label,
                            codename=codename,
                        ).pk
                    )
                except Permission.DoesNotExist as exc:
                    raise ValueError(f"Unknown permission: {app_label}.{codename}") from exc

            group.permissions.set(permission_ids)
            return group

    def grant_all_permissions(self, group_name):
        """Grant every Django permission currently installed in this tenant schema."""
        with schema_context(self.schema_name):
            group, _ = Group.objects.get_or_create(name=group_name)
            group.permissions.add(*Permission.objects.all())
            return group

    def permissions_for_group(self, group_name):
        with schema_context(self.schema_name):
            group = Group.objects.get(name=group_name)
            return list(
                group.permissions.select_related("content_type")
                .order_by("content_type__app_label", "codename")
                .values_list("content_type__app_label", "codename")
            )

    def has_permission(self, user, permission):
        email = getattr(user, "email", user)
        with schema_context(self.schema_name):
            tenant_user = User.objects.get(email__iexact=email)
            return tenant_user.has_perm(permission)
