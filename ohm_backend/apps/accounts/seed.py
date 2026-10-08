from .models import Role

# Current hierarchy: Tenant Admin > Customer / User. Sub-roles (Venue/Service/Resource Admin,
# Ops Manager, Staff) are defined in Role.Code but seeded only when those features ship.
ROLES = [
    ("TENANT_ADMIN", "Tenant Admin", 100),
    ("CUSTOMER", "Customer / User", 10),
]


def seed_roles():
    for code, name, rank in ROLES:
        Role.objects.update_or_create(code=code, defaults={"name": name, "rank": rank})
