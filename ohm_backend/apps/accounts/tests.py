from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from apps.accounts.models import User
from apps.accounts.permissions import (
    CanManageRoles,
    DynamicModelPermissions,
    IsSchemaAdministrator,
    is_root_admin,
    is_root_user,
    is_superadmin,
    is_tenant_admin,
)
from apps.accounts.serializers import (
    GroupSerializer,
    PermissionSerializer,
    UserSerializer,
)
from apps.accounts.tenant_views import GroupViewSet, PermissionViewSet
from apps.platform.models import PlatformAdmin


class DummyView:
    def __init__(self, model):
        self.queryset = model.objects.all()

    def get_queryset(self):
        return self.queryset


class DjangoGroupsAndPermissionsTest(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.public_schema = get_public_schema_name()

        # Seed essential groups
        self.super_group, _ = Group.objects.get_or_create(name="Super User")
        self.tenant_admin_group, _ = Group.objects.get_or_create(name="Tenant Admin")
        self.customer_group, _ = Group.objects.get_or_create(name="CUSTOMER")

        # Users
        self.superuser = User.objects.create(
            email="superadmin@example.com",
            is_superuser=True,
            is_staff=True,
            is_active=True,
        )
        self.superuser.groups.add(self.super_group)

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
        self.tenant_admin_user.groups.add(self.tenant_admin_group)

        self.regular_user = User.objects.create(
            email="customer@example.com",
            is_superuser=False,
            is_staff=False,
            is_active=True,
        )
        self.regular_user.groups.add(self.customer_group)

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

    def test_is_root_admin(self):
        self.assertTrue(is_root_admin(self.superuser))
        self.assertTrue(is_root_admin(self.root_staff))
        self.assertTrue(is_root_admin(self.platform_admin_user))
        self.assertFalse(is_root_admin(self.regular_user))

    def test_root_admin_has_tenant_admin_privileges(self):
        self.assertTrue(is_tenant_admin(self.superuser))
        self.assertTrue(is_tenant_admin(self.root_staff))
        self.assertTrue(is_tenant_admin(self.platform_admin_user))
        self.assertFalse(is_tenant_admin(self.regular_user))

    def test_can_manage_roles_permission(self):
        perm = CanManageRoles()
        view = GroupViewSet()

        req_super = self.factory.get("/api/groups/")
        req_super.user = self.superuser
        self.assertTrue(perm.has_permission(req_super, view))

        req_root = self.factory.get("/api/groups/")
        req_root.user = self.root_staff
        self.assertTrue(perm.has_permission(req_root, view))

        req_pa = self.factory.get("/api/groups/")
        req_pa.user = self.platform_admin_user
        self.assertTrue(perm.has_permission(req_pa, view))

        req_cust = self.factory.get("/api/groups/")
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

        # Explicitly verify _check_perm checks user.groups directly
        self.assertTrue(perm_checker._check_perm(user, "accounts.view_user"))
        self.assertFalse(perm_checker._check_perm(user, "accounts.delete_user"))

    def test_permission_serializer(self):
        serializer = PermissionSerializer(self.view_user_perm)
        data = serializer.data
        self.assertEqual(data["codename"], "view_user")
        self.assertEqual(data["app_label"], "accounts")
        self.assertEqual(data["model"], "user")

    def test_group_viewset_crud(self):
        # 1. Create group via ViewSet
        create_view = GroupViewSet.as_view({"post": "create"})
        req_post = self.factory.post("/api/groups/", {
            "name": "Clinical Pharmacists",
            "permissions": [self.view_user_perm.id, self.add_user_perm.id],
        }, format="json")
        force_authenticate(req_post, user=self.superuser)
        create_resp = create_view(req_post)
        self.assertEqual(create_resp.status_code, 201)
        group_id = create_resp.data["id"]
        self.assertEqual(create_resp.data["name"], "Clinical Pharmacists")
        self.assertEqual(set(create_resp.data["permissions"]), {self.view_user_perm.id, self.add_user_perm.id})
        self.assertEqual(len(create_resp.data["permission_details"]), 2)
        self.assertEqual(create_resp.data["user_count"], 0)

        # 2. Retrieve group
        retrieve_view = GroupViewSet.as_view({"get": "retrieve"})
        req_get = self.factory.get(f"/api/groups/{group_id}/")
        force_authenticate(req_get, user=self.superuser)
        retrieve_resp = retrieve_view(req_get, pk=group_id)
        self.assertEqual(retrieve_resp.status_code, 200)
        self.assertEqual(retrieve_resp.data["name"], "Clinical Pharmacists")

        # 3. Update group
        update_view = GroupViewSet.as_view({"patch": "partial_update"})
        req_patch = self.factory.patch(f"/api/groups/{group_id}/", {
            "name": "Senior Pharmacists",
            "permissions": [self.view_user_perm.id],
        }, format="json")
        force_authenticate(req_patch, user=self.superuser)
        patch_resp = update_view(req_patch, pk=group_id)
        self.assertEqual(patch_resp.status_code, 200)
        self.assertEqual(patch_resp.data["name"], "Senior Pharmacists")
        self.assertEqual(patch_resp.data["permissions"], [self.view_user_perm.id])

        # 4. Delete group
        destroy_view = GroupViewSet.as_view({"delete": "destroy"})
        req_del = self.factory.delete(f"/api/groups/{group_id}/")
        force_authenticate(req_del, user=self.superuser)
        del_resp = destroy_view(req_del, pk=group_id)
        self.assertEqual(del_resp.status_code, 204)
        self.assertFalse(Group.objects.filter(pk=group_id).exists())

    def test_builtin_groups_cannot_be_deleted(self):
        super_group, _ = Group.objects.get_or_create(name="Super User")

        destroy_view = GroupViewSet.as_view({"delete": "destroy"})
        req_del = self.factory.delete(f"/api/groups/{super_group.id}/")
        force_authenticate(req_del, user=self.superuser)

        resp = destroy_view(req_del, pk=super_group.id)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Built-in permission groups cannot be deleted.", str(resp.data))

    def test_group_users_action(self):
        group = Group.objects.create(name="Head Nurses")
        users_view = GroupViewSet.as_view({
            "get": "users",
            "post": "users",
            "delete": "users",
        })

        # 1. Add user to group via POST /api/groups/{id}/users/
        req_add = self.factory.post(f"/api/groups/{group.id}/users/", {
            "user_ids": [self.regular_user.id],
        }, format="json")
        force_authenticate(req_add, user=self.superuser)
        add_resp = users_view(req_add, pk=group.id)
        self.assertEqual(add_resp.status_code, 200)
        self.assertTrue(self.regular_user.groups.filter(id=group.id).exists())

        # 2. List users in group via GET /api/groups/{id}/users/
        req_list = self.factory.get(f"/api/groups/{group.id}/users/")
        force_authenticate(req_list, user=self.superuser)
        list_resp = users_view(req_list, pk=group.id)
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(len(list_resp.data), 1)
        self.assertEqual(list_resp.data[0]["email"], self.regular_user.email)

        # 3. Remove user from group via DELETE /api/groups/{id}/users/
        req_rem = self.factory.delete(f"/api/groups/{group.id}/users/", {
            "user_id": self.regular_user.id,
        }, format="json")
        force_authenticate(req_rem, user=self.superuser)
        rem_resp = users_view(req_rem, pk=group.id)
        self.assertEqual(rem_resp.status_code, 204)
        self.assertFalse(self.regular_user.groups.filter(id=group.id).exists())

    def test_permission_viewset_grouped(self):
        grouped_view = PermissionViewSet.as_view({"get": "grouped"})
        req = self.factory.get("/api/permissions/grouped/")
        force_authenticate(req, user=self.superuser)
        resp = grouped_view(req)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("accounts", resp.data)
        self.assertIn("user", resp.data["accounts"])
        codenames = [p["codename"] for p in resp.data["accounts"]["user"]]
        self.assertIn("view_user", codenames)
        self.assertIn("add_user", codenames)

    def test_user_serializer_mirroring_django_admin(self):
        group = Group.objects.create(name="Triage Team")
        group.permissions.add(self.view_user_perm)

        # Create user with groups and direct user_permissions
        user_data = {
            "email": "triage_nurse@example.com",
            "password": "SecurePassword123!",
            "first_name": "Triage",
            "last_name": "Nurse",
            "groups": [group.id],
            "user_permissions": [self.change_user_perm.id],
        }
        req = self.factory.post("/api/users/")
        req.user = self.superuser

        serializer = UserSerializer(data=user_data, context={"request": req})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        created_user = serializer.save()

        self.assertEqual(list(created_user.groups.all()), [group])
        self.assertEqual(list(created_user.user_permissions.all()), [self.change_user_perm])

        # Verify permissions: view_user from group, change_user from user_permissions
        self.assertTrue(created_user.has_perm("accounts.view_user"))
        self.assertTrue(created_user.has_perm("accounts.change_user"))
        self.assertFalse(created_user.has_perm("accounts.delete_user"))

        # Verify serialized output
        out = UserSerializer(created_user).data
        self.assertEqual(out["groups"], [group.id])
        self.assertEqual(out["group_details"][0]["name"], "Triage Team")
        self.assertEqual(out["user_permissions"], [self.change_user_perm.id])
        self.assertEqual(out["user_permission_details"][0]["codename"], "change_user")

        # Update user permissions and groups
        update_data = {
            "groups": [],
            "user_permissions": [self.delete_user_perm.id],
        }
        update_serializer = UserSerializer(
            instance=created_user,
            data=update_data,
            partial=True,
            context={"request": req},
        )
        self.assertTrue(update_serializer.is_valid(), update_serializer.errors)
        update_serializer.save()

        # Re-fetch from DB to get fresh permissions
        updated_user = User.objects.get(pk=created_user.pk)
        self.assertEqual(updated_user.groups.count(), 0)
        self.assertEqual(list(updated_user.user_permissions.all()), [self.delete_user_perm])
        self.assertTrue(updated_user.has_perm("accounts.delete_user"))
        self.assertFalse(updated_user.has_perm("accounts.view_user"))

    def test_guard_last_tenant_admin_with_groups(self):
        admin_group, _ = Group.objects.get_or_create(name="Tenant Admin")
        self.tenant_admin_user.groups.add(admin_group)

        req = self.factory.patch("/api/users/")
        req.user = self.superuser

        # Attempt to remove Tenant Admin from the only active tenant admin
        update_data = {"groups": []}
        serializer = UserSerializer(
            instance=self.tenant_admin_user,
            data=update_data,
            partial=True,
            context={"request": req},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(ValidationError) as ctx:
            serializer.save()
        self.assertIn("A workspace must keep at least one active Tenant Admin.", str(ctx.exception))


class SuperUserTenantAccessTestCase(TenantTestCase):
    @classmethod
    def setup_tenant(cls, tenant):
        tenant.name = "Health Provider"
        tenant.status = "active"

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()
        self.client = APIClient()
        self.public_schema = get_public_schema_name()

        # Create superuser and regular user in public schema
        with schema_context(self.public_schema):
            self.superuser = User.objects.create_user(
                email="superadmin@example.com",
                password="SuperSecretPassword123!",
                first_name="Super",
                last_name="Admin",
                is_superuser=True,
                is_staff=True,
                is_active=True,
            )
            super_group, _ = Group.objects.get_or_create(name="Super User")
            self.superuser.groups.add(super_group)

            self.root_staff = User.objects.create_user(
                email="rootstaff@example.com",
                password="StaffPassword123!",
                first_name="Root",
                last_name="Staff",
                is_superuser=False,
                is_staff=True,
                is_active=True,
            )

            self.regular_public_user = User.objects.create_user(
                email="publicguy@example.com",
                password="Password123!",
                first_name="Public",
                last_name="Guy",
                is_superuser=False,
                is_staff=False,
                is_active=True,
            )

    def test_superuser_can_login_to_tenant_schema_with_creds(self):
        # Superuser logs in targeting the tenant workspace using their credentials
        res = self.client.post(
            "/api/auth/login/",
            {
                "username": "superadmin@example.com",
                "password": "SuperSecretPassword123!",
                "method": "password",
            },
            format="json",
            HTTP_X_TENANT=self.tenant.schema_name,
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["schema_name"], self.tenant.schema_name)
        self.assertIn("tokens", res.data)
        access_token = res.data["tokens"]["access"]
        self.assertTrue(bool(access_token))

        # Verify superuser was mirrored into tenant schema with full superuser privileges
        tenant_user = User.objects.get(email="superadmin@example.com")
        self.assertTrue(tenant_user.is_superuser)
        self.assertTrue(tenant_user.is_staff)
        self.assertTrue(tenant_user.is_active)
        self.assertTrue(tenant_user.is_owner)
        self.assertTrue(tenant_user.is_manager)

        # Verify tenant-issued access token can access tenant-scoped resources
        group_res = self.client.get(
            "/api/groups/",
            HTTP_X_TENANT=self.tenant.schema_name,
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )
        self.assertEqual(group_res.status_code, 200, group_res.data)

        # Superuser can create groups in tenant schema
        create_res = self.client.post(
            "/api/groups/",
            {"name": "Emergency Doctors", "permissions": []},
            format="json",
            HTTP_X_TENANT=self.tenant.schema_name,
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )
        self.assertEqual(create_res.status_code, 201, create_res.data)

    def test_superuser_public_token_can_access_tenant_apis(self):
        # Superuser logs into public schema (no X-Tenant header)
        login_res = self.client.post(
            "/api/auth/login/",
            {
                "username": "superadmin@example.com",
                "password": "SuperSecretPassword123!",
                "method": "password",
            },
            format="json",
        )
        self.assertEqual(login_res.status_code, 200)
        self.assertEqual(login_res.data["schema_name"], self.public_schema)
        public_token = login_res.data["tokens"]["access"]

        # Superuser accesses tenant workspace API with their public token and X-Tenant header
        tenant_api_res = self.client.get(
            "/api/groups/",
            HTTP_X_TENANT=self.tenant.schema_name,
            HTTP_AUTHORIZATION=f"Bearer {public_token}",
        )
        self.assertEqual(tenant_api_res.status_code, 200, tenant_api_res.data)

    def test_superuser_wrong_password_rejected_on_tenant(self):
        res = self.client.post(
            "/api/auth/login/",
            {
                "username": "superadmin@example.com",
                "password": "WrongPassword!",
                "method": "password",
            },
            format="json",
            HTTP_X_TENANT=self.tenant.schema_name,
        )
        self.assertEqual(res.status_code, 401)

    def test_non_superuser_public_user_cannot_login_to_tenant(self):
        res = self.client.post(
            "/api/auth/login/",
            {
                "username": "publicguy@example.com",
                "password": "Password123!",
                "method": "password",
            },
            format="json",
            HTTP_X_TENANT=self.tenant.schema_name,
        )
        self.assertEqual(res.status_code, 401)

    def test_root_staff_can_login_to_tenant_schema_with_creds(self):
        res = self.client.post(
            "/api/auth/login/",
            {
                "username": "rootstaff@example.com",
                "password": "StaffPassword123!",
                "method": "password",
            },
            format="json",
            HTTP_X_TENANT=self.tenant.schema_name,
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["schema_name"], self.tenant.schema_name)
        self.assertIn("tokens", res.data)
        access_token = res.data["tokens"]["access"]

        tenant_user = User.objects.get(email="rootstaff@example.com")
        self.assertTrue(tenant_user.is_superuser)
        self.assertTrue(tenant_user.is_staff)

        group_res = self.client.get(
            "/api/groups/",
            HTTP_X_TENANT=self.tenant.schema_name,
            HTTP_AUTHORIZATION=f"Bearer {access_token}",
        )
        self.assertEqual(group_res.status_code, 200, group_res.data)

    def test_root_staff_public_token_can_access_tenant_apis(self):
        login_res = self.client.post(
            "/api/auth/login/",
            {
                "username": "rootstaff@example.com",
                "password": "StaffPassword123!",
                "method": "password",
            },
            format="json",
        )
        self.assertEqual(login_res.status_code, 200)
        self.assertEqual(login_res.data["schema_name"], self.public_schema)
        public_token = login_res.data["tokens"]["access"]

        tenant_api_res = self.client.get(
            "/api/groups/",
            HTTP_X_TENANT=self.tenant.schema_name,
            HTTP_AUTHORIZATION=f"Bearer {public_token}",
        )
        self.assertEqual(tenant_api_res.status_code, 200, tenant_api_res.data)
