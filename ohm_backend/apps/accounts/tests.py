from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import RequestFactory, TestCase
from django_tenants.utils import get_public_schema_name
from rest_framework.exceptions import ValidationError

from apps.accounts.models import Role, RoleAssignment, User
from apps.accounts.permissions import (
    CanManageRoles,
    DynamicModelPermissions,
    IsSchemaAdministrator,
    is_root_user,
    is_superadmin,
    is_tenant_admin,
)
from apps.accounts.serializers import (
    GroupSerializer,
    PermissionSerializer,
    RoleAssignmentSerializer,
    RoleSerializer,
    UserSerializer,
)
from apps.accounts.tenant_views import GroupViewSet, PermissionViewSet, RoleAssignmentViewSet, RoleViewSet
from apps.platform.models import PlatformAdmin


class DummyView:
    def __init__(self, model):
        self.queryset = model.objects.all()

    def get_queryset(self):
        return self.queryset


class DjangoGroupsAndPermissionsTest(TestCase):
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

        # Django Permissions
        self.user_ct = ContentType.objects.get_for_model(User)
        self.view_user_perm = Permission.objects.get(content_type=self.user_ct, codename="view_user")
        self.add_user_perm = Permission.objects.get(content_type=self.user_ct, codename="add_user")
        self.change_user_perm = Permission.objects.get(content_type=self.user_ct, codename="change_user")
        self.delete_user_perm = Permission.objects.get(content_type=self.user_ct, codename="delete_user")

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

        req_super = self.factory.get("/api/roles/")
        req_super.user = self.superuser
        self.assertTrue(perm.has_permission(req_super, view))

        req_root = self.factory.get("/api/roles/")
        req_root.user = self.root_staff
        self.assertTrue(perm.has_permission(req_root, view))

        req_pa = self.factory.get("/api/roles/")
        req_pa.user = self.platform_admin_user
        self.assertTrue(perm.has_permission(req_pa, view))

        req_cust = self.factory.get("/api/roles/")
        req_cust.user = self.regular_user
        self.assertFalse(perm.has_permission(req_cust, view))

    def test_create_and_manage_django_group(self):
        # Create group with permissions
        group_data = {
            "name": "User Managers",
            "permissions": [self.view_user_perm.id, self.change_user_perm.id],
        }
        serializer = GroupSerializer(data=group_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        group = serializer.save()

        self.assertEqual(group.name, "User Managers")
        self.assertEqual(group.permissions.count(), 2)
        self.assertIn(self.view_user_perm, group.permissions.all())
        self.assertIn(self.change_user_perm, group.permissions.all())

        # Check serialized output has permission_details
        out = GroupSerializer(group).data
        details = out["permission_details"]
        codenames = [d["codename"] for d in details]
        self.assertIn("view_user", codenames)
        self.assertIn("change_user", codenames)

        # Update group permissions
        update_serializer = GroupSerializer(
            instance=group,
            data={"name": "User Viewers", "permissions": [self.view_user_perm.id]},
        )
        self.assertTrue(update_serializer.is_valid(), update_serializer.errors)
        updated_group = update_serializer.save()
        self.assertEqual(updated_group.name, "User Viewers")
        self.assertEqual(updated_group.permissions.count(), 1)
        self.assertEqual(list(updated_group.permissions.all()), [self.view_user_perm])

    def test_user_group_assignment_and_permissions(self):
        group = Group.objects.create(name="Staff Viewer")
        group.permissions.add(self.view_user_perm)

        # Initially regular_user cannot view user
        self.assertFalse(self.regular_user.has_perm("accounts.view_user"))

        # Assign group to user
        self.regular_user.groups.add(group)

        # Refetch from DB to clear cached permissions
        user = User.objects.get(pk=self.regular_user.pk)
        self.assertTrue(user.has_perm("accounts.view_user"))
        self.assertFalse(user.has_perm("accounts.change_user"))

    def test_dynamic_model_permissions_with_django_groups(self):
        group = Group.objects.create(name="Account Viewers")
        group.permissions.add(self.view_user_perm)

        perm_checker = DynamicModelPermissions()
        view = DummyView(User)

        # GET request without group permission -> denied
        req_get = self.factory.get("/api/users/")
        req_get.user = self.regular_user
        self.assertFalse(perm_checker.has_permission(req_get, view))

        # Assign group
        self.regular_user.groups.add(group)
        user = User.objects.get(pk=self.regular_user.pk)
        req_get.user = user

        # Now GET request -> allowed via Django permission group!
        self.assertTrue(perm_checker.has_permission(req_get, view))

        # POST request (requires add_user) -> still denied
        req_post = self.factory.post("/api/users/")
        req_post.user = user
        self.assertFalse(perm_checker.has_permission(req_post, view))

        # Superadmin and Tenant admin are always allowed
        req_post.user = self.superuser
        self.assertTrue(perm_checker.has_permission(req_post, view))

    def test_permission_serializer(self):
        serializer = PermissionSerializer(self.view_user_perm)
        data = serializer.data
        self.assertEqual(data["codename"], "view_user")
        self.assertEqual(data["app_label"], "accounts")
        self.assertEqual(data["model"], "user")

    def test_role_does_not_contain_permissions(self):
        serializer = RoleSerializer(
            data={"code": "SENIOR_NURSE", "name": "Senior Nurse", "rank": 40}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        role = serializer.save()
        self.assertEqual(role.code, "SENIOR_NURSE")
        self.assertFalse(hasattr(role, "permissions"))

        # Serialized data has no permissions field
        data = RoleSerializer(role).data
        self.assertNotIn("permissions", data)
        self.assertNotIn("permission_details", data)

    def test_builtin_roles_cannot_be_deleted(self):
        view = RoleViewSet()
        req = self.factory.delete(f"/api/roles/{self.super_role.pk}/")
        req.user = self.superuser
        view.request = req
        view.get_object = lambda: self.super_role

        with self.assertRaises(ValidationError) as ctx:
            view.destroy(req)
        self.assertIn("Built-in roles cannot be deleted.", str(ctx.exception))
