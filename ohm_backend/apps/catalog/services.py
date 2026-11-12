from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404

from .constants import EntityType
from .models import Photos, Resource, Service, Venue

User = get_user_model()


class PhotoService:
    @staticmethod
    def get_model_and_content_type(entity_type: str):
        type_str = entity_type.lower()
        if type_str == EntityType.VENUE:
            return Venue, ContentType.objects.get_for_model(Venue)
        elif type_str == EntityType.SERVICE:
            return Service, ContentType.objects.get_for_model(Service)
        elif type_str == EntityType.RESOURCE:
            return Resource, ContentType.objects.get_for_model(Resource)
        raise ValidationError(f"Invalid entity type: '{entity_type}'. Must be one of: venue, service, resource.")

    @classmethod
    def attach_photo(cls, entity_type: str, object_id: int, image_file, is_primary: bool = False):
        model_cls, content_type = cls.get_model_and_content_type(entity_type)
        entity = get_object_or_404(model_cls, id=object_id)

        with transaction.atomic():
            if is_primary:
                Photos.objects.filter(
                    content_type=content_type,
                    object_id=object_id,
                    is_primary=True,
                ).update(is_primary=False)

            photo = Photos.objects.create(
                image=image_file,
                is_primary=is_primary,
                content_type=content_type,
                object_id=object_id,
            )
        return photo, entity

    @classmethod
    def set_primary(cls, photo_id: int):
        with transaction.atomic():
            photo = get_object_or_404(Photos, id=photo_id)
            Photos.objects.filter(
                content_type=photo.content_type,
                object_id=photo.object_id,
                is_primary=True,
            ).update(is_primary=False)
            photo.is_primary = True
            photo.save(update_fields=["is_primary"])
        return photo

    @classmethod
    def delete_photo(cls, photo_id: int):
        photo = get_object_or_404(Photos, id=photo_id)
        if photo.image:
            photo.image.delete(save=False)
        photo.delete()
        return True


class EntityAssignmentService:
    ENTITY_MODELS = {
        EntityType.VENUE: Venue,
        EntityType.SERVICE: Service,
        EntityType.RESOURCE: Resource,
    }

    @classmethod
    def get_entity(cls, entity_type: str, entity_id: int):
        model_cls = cls.ENTITY_MODELS.get(entity_type.lower())
        if not model_cls:
            raise ValidationError(f"Invalid entity type '{entity_type}'.")
        return get_object_or_404(model_cls, id=entity_id)

    @classmethod
    def assign_users(cls, entity_type: str, entity_id: int, employee_ids: list[int]):
        entity = cls.get_entity(entity_type, entity_id)
        employees = User.objects.filter(id__in=employee_ids, is_active=True)

        found_ids = set(employees.values_list("id", flat=True))
        missing = set(employee_ids) - found_ids
        if missing:
            raise ValidationError(f"Employees not found with IDs: {', '.join(map(str, missing))}")

        managers_to_add = []
        staff_to_add = []

        for emp in employees:
            if getattr(emp, "is_manager", False) or emp.role_assignments.filter(
                role__code__in=["OPS_MANAGER", "VENUE_ADMIN", "SERVICE_ADMIN", "RESOURCE_ADMIN"]
            ).exists():
                managers_to_add.append(emp)
            else:
                staff_to_add.append(emp)

        with transaction.atomic():
            if managers_to_add:
                entity.manager.add(*managers_to_add)
            if staff_to_add:
                entity.staff.add(*staff_to_add)

        return {
            "entity_id": entity.id,
            "entity_type": entity_type,
            "assigned_managers": [m.id for m in managers_to_add],
            "assigned_staff": [s.id for s in staff_to_add],
        }

    @classmethod
    def unassign_users(cls, entity_type: str, entity_id: int, employee_ids: list[int]):
        entity = cls.get_entity(entity_type, entity_id)
        employees = User.objects.filter(id__in=employee_ids)

        with transaction.atomic():
            entity.manager.remove(*employees)
            entity.staff.remove(*employees)

        return {
            "entity_id": entity.id,
            "entity_type": entity_type,
            "unassigned_user_ids": list(employees.values_list("id", flat=True)),
        }
