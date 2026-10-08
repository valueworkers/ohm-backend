from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import EmployeeProfile, Permission, Role, RoleAssignment, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """In the public admin this manages platform users (Super Admin / O-HM team)."""

    ordering = ("email",)
    list_display = ("email", "full_name", "is_active", "is_staff", "is_superuser")
    search_fields = ("email", "full_name")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("full_name", "phone")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "password1", "password2", "is_staff", "is_superuser")}),
    )


admin.site.register((Role, Permission, RoleAssignment, EmployeeProfile))
