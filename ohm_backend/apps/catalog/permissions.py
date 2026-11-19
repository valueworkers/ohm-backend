from rest_framework.permissions import BasePermission, SAFE_METHODS
from apps.accounts.permissions import is_tenant_admin, is_superadmin, is_root_user


def can_manage_entity(user, obj):
    """Determine whether a user can manage a Venue, Service, or Resource."""
    if not (user and user.is_authenticated):
        return False
    if is_superadmin(user) or is_root_user(user) or is_tenant_admin(user):
        return True
    if getattr(user, "is_owner", False):
        return True
    if hasattr(obj, "owner") and obj.owner_id == user.id:
        return True
    if hasattr(obj, "manager") and obj.manager.filter(id=user.id).exists():
        return True
    return False


class EntityAccessPermission(BasePermission):
    """
    Object-level permission allowing read-only access for safe methods (if public/active),
    and requiring owner, manager, or admin rights for state modifications.
    """

    def has_permission(self, request, view):
        if request.user and (is_superadmin(request.user) or is_root_user(request.user)):
            return True
        if request.method in SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.user and (is_superadmin(request.user) or is_root_user(request.user)):
            return True
        if request.method in SAFE_METHODS:
            return True
        return can_manage_entity(request.user, obj)


class CanAssignUsers(BasePermission):
    """
    Permission allowing owners, managers, tenant admins, root users, and superadmins
    to assign or unassign staff/managers to entities.
    """

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return bool(
            user.is_superuser
            or is_superadmin(user)
            or is_root_user(user)
            or is_tenant_admin(user)
            or getattr(user, "is_owner", False)
            or getattr(user, "is_manager", False)
        )
