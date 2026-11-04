from django.contrib.auth.models import Group, Permission
from django.db import connection
from django.test import RequestFactory, TestCase
from django_tenants.utils import get_public_schema_name
from rest_framework.exceptions import ValidationError

from apps.accounts.models import Role, RoleAssignment, User
from apps.accounts.permissions import (
    CanManageRoles,
    IsSchemaAdministrator,
    is_root_user,
    is_superadmin,
    is_tenant_admin,
)
from apps.accounts.serializers import RoleAssignmentSerializer, RoleSerializer
from apps.accounts.tenant_views import RoleAssignmentViewSet, RoleViewSet
from apps.platform.models import PlatformAdmin


class RoleManagementPermissionsTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.public_schema = get_public_schema_name()

        # Seed essential roles
        self.super_role, _ = Role.objects.get_or_create(
            code=Role.Code.SUPER_ADMIN,
            defaults={"name": "Super Admin", "rank": 100},
        )
        self.tenant_admin_role, _ = Role.objects.get_or_create(
            code=Role.Code.TENANT_ADMIN,
            defaults={"name": "Tenant Admin", "rank": 80},
        )
        self.customer_role, _ = Role.objects.get_or_create(
            code=Role.Code.CUSTOMER,
            defaults={"name": "Customer", "rank": 10},
        )

        # Users
        self.superuser = User.objects.create(
            email="superadmin@example.com",
            is_superuser=True,
            is_staff=True,
            is_active=True,
        )
        self.root_staff = User.objects.create(
            email="rootstaff@example.com",
            is_superuser=False,
            is_staff=True,
            is_active=True,
        )
        self.platform_admin_user = User.objects.create(
            email="platformadmin@example.com",
            is_superuser=False,
            is_staff=False,
            is_active=True,
        )
        PlatformAdmin.objects.create(user=self.platform_admin_user, title="Ops Admin")

        self.tenant_admin_user = User.objects.create(
            email="tenantadmin@example.com",
            is_superuser=False,
            is_staff=False,
            is_active=True,
        )
        RoleAssignment.objects.create(
            user=self.tenant_admin_user,
            role=self.tenant_admin_role,
        )

        self.regular_user = User.objects.create(
            email="customer@example.com",
            is_superuser=False,
            is_staff=False,
            is_active=True,
        )
        RoleAssignment.objects.create(
            user=self.regular_user,
            role=self.customer_role,
        )

    def test_is_superadmin(self):
        self.assertTrue(is_superadmin(self.superuser))
        self.assertFalse(is_superadmin(self.root_staff))
        self.assertFalse(is_superadmin(self.regular_user))

    def test_is_root_user(self):
        self.assertTrue(is_root_user(self.root_staff))
        self.assertTrue(is_root_user(self.platform_admin_user))
        self.assertFalse(is_root_user(self.regular_user))

    def test_can_manage_roles_permission(self):
        perm = CanManageRoles()
        view = RoleViewSet()

        # Superadmin has permission
        req_super = self.factory.get("/api/roles/")
        req_super.user = self.superuser
        self.assertTrue(perm.has_permission(req_super, view))

        # Root staff has permission
        req_root = self.factory.get("/api/roles/")
        req_root.user = self.root_staff
        self.assertTrue(perm.has_permission(req_root, view))

        # PlatformAdmin profile user has permission
        req_pa = self.factory.get("/api/roles/")
        req_pa.user = self.platform_admin_user
        self.assertTrue(perm.has_permission(req_pa, view))

        # Regular customer denied
        req_cust = self.factory.get("/api/roles/")
        req_cust.user = self.regular_user
        self.assertFalse(perm.has_permission(req_cust, view))

    def test_builtin_roles_cannot_be_deleted(self):
        view = RoleViewSet()
        req = self.factory.delete(f"/api/roles/{self.super_role.pk}/")
        req.user = self.superuser
        view.request = req
        view.get_object = lambda: self.super_role

        with self.assertRaises(ValidationError) as ctx:
            view.destroy(req)
        self.assertIn("Built-in roles cannot be deleted.", str(ctx.exception))

    def test_cannot_assign_super_admin_role_as_non_superuser(self):
        req = self.factory.post("/api/role-assignments/")
        req.user = self.root_staff

        serializer = RoleAssignmentSerializer(
            data={"user_id": self.regular_user.pk, "role_id": self.super_role.pk},
            context={"request": req},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("role_id", serializer.errors)
        self.assertIn("Only a Superadmin can assign the Super Admin role.", str(serializer.errors["role_id"]))

    def test_superadmin_can_assign_super_admin_role(self):
        req = self.factory.post("/api/role-assignments/")
        req.user = self.superuser

        target_user = User.objects.create(email="newsuper@example.com", is_active=True)
        serializer = RoleAssignmentSerializer(
            data={"user_id": target_user.pk, "role_id": self.super_role.pk},
            context={"request": req},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        assignment = serializer.save()
        self.assertEqual(assignment.role, self.super_role)
        self.assertEqual(assignment.user, target_user)

    def test_cannot_delete_last_active_superadmin_assignment(self):
        assignment = RoleAssignment.objects.get(
            user=self.superuser,
            role=self.super_role,
            scope_type__isnull=True,
        )
        view = RoleAssignmentViewSet()
        with self.assertRaises(ValidationError) as ctx:
            view.perform_destroy(assignment)
        self.assertIn("The platform must keep at least one active Super Admin.", str(ctx.exception))

    def test_create_custom_role_with_reserved_code_fails(self):
        serializer = RoleSerializer(
            data={"code": "TENANT_ADMIN", "name": "Fake Tenant Admin", "rank": 50, "permissions": []}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("code", serializer.errors)
        self.assertIn("This code is reserved for a built-in role.", str(serializer.errors["code"]))

        serializer2 = RoleSerializer(
            data={"code": "SUPER_ADMIN", "name": "Fake Super Admin", "rank": 50, "permissions": []}
        )
        self.assertFalse(serializer2.is_valid())
        self.assertIn("code", serializer2.errors)
        self.assertIn("This code is reserved for a built-in role.", str(serializer2.errors["code"]))

    def test_create_valid_custom_role(self):
        serializer = RoleSerializer(
            data={"code": "SENIOR_NURSE", "name": "Senior Nurse", "rank": 40, "permissions": []}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        role = serializer.save()
        self.assertEqual(role.code, "SENIOR_NURSE")
        self.assertEqual(role.name, "Senior Nurse")

    def test_tenant_admin_permission_in_tenant_vs_public(self):
        perm = CanManageRoles()
        view = RoleViewSet()

        # In public schema, tenant admin user is denied
        req = self.factory.get("/api/roles/")
        req.user = self.tenant_admin_user
        self.assertFalse(perm.has_permission(req, view))

        # In a tenant schema, tenant admin is permitted
        with connection.cursor() as cursor:
            # Fake schema context switch
            original_schema = connection.schema_name
            try:
                connection.schema_name = "test_tenant"
                self.assertTrue(perm.has_permission(req, view))
            finally:
                connection.schema_name = original_schema
