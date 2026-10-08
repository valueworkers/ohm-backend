from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from rest_framework.permissions import BasePermission

from .models import RoleAssignment


def has_role(user, *, codes=None, min_rank=None, scope=None):
    """True if user holds a matching role. Tenant-wide assignments satisfy any scope;
    scoped assignments only satisfy their own object."""
    if not (user and user.is_authenticated):
        return False
    qs = RoleAssignment.objects.filter(user=user)
    if codes:
        qs = qs.filter(role__code__in=codes)
    if min_rank is not None:
        qs = qs.filter(role__rank__gte=min_rank)
    if scope is not None:
        ct = ContentType.objects.get_for_model(scope)
        qs = qs.filter(Q(scope_type__isnull=True) | Q(scope_type=ct, scope_id=scope.pk))
    return qs.exists()


def RequireRole(*codes):
    class _RequireRole(BasePermission):
        def has_permission(self, request, view):
            return has_role(request.user, codes=codes)

    return _RequireRole


def MinRank(rank):
    class _MinRank(BasePermission):
        def has_permission(self, request, view):
            return has_role(request.user, min_rank=rank)

    return _MinRank


IsTenantAdmin = RequireRole("TENANT_ADMIN")
