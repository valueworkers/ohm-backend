import io
from decimal import Decimal
from PIL import Image

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django_tenants.test.cases import TenantTestCase
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.models import Role, RoleAssignment
from apps.billing.constants import BookingType
from apps.billing.models import Location
from apps.catalog.models import Photos, Resource, Service, Venue
from apps.catalog.services import EntityAssignmentService, PhotoService

User = get_user_model()


def create_test_image(filename="test.png", color="blue"):
    file_io = io.BytesIO()
    img = Image.new("RGB", (100, 100), color=color)
    img.save(file_io, format="PNG")
    file_io.seek(0)
    return SimpleUploadedFile(filename, file_io.read(), content_type="image/png")


class CatalogE2ETestCase(TenantTestCase):
    @classmethod
    def setup_tenant(cls, tenant):
        tenant.name = "Test Healthcare Tenant"
        tenant.status = "active"
        tenant.modules = ["billing", "catalog"]

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.defaults["HTTP_X_TENANT"] = self.tenant.schema_name

        # Create roles
        self.role_admin, _ = Role.objects.get_or_create(
            code=Role.Code.TENANT_ADMIN,
            defaults={"name": "Tenant Admin", "rank": 100},
        )
        self.role_manager, _ = Role.objects.get_or_create(
            code=Role.Code.VENUE_ADMIN,
            defaults={"name": "Venue Admin", "rank": 80},
        )
        self.role_staff, _ = Role.objects.get_or_create(
            code=Role.Code.STAFF,
            defaults={"name": "Staff Member", "rank": 20},
        )

        # Create users
        self.admin_user = User.objects.create_user(
            email="admin@example.com",
            password="Password123!",
            first_name="Admin",
            last_name="User",
        )
        RoleAssignment.objects.create(user=self.admin_user, role=self.role_admin)

        self.owner_user = User.objects.create_user(
            email="owner@example.com",
            password="Password123!",
            first_name="Owner",
            last_name="User",
        )
        RoleAssignment.objects.create(user=self.owner_user, role=self.role_admin)

        self.manager_user = User.objects.create_user(
            email="manager@example.com",
            password="Password123!",
            first_name="Manager",
            last_name="User",
        )
        RoleAssignment.objects.create(user=self.manager_user, role=self.role_manager)

        self.staff_user = User.objects.create_user(
            email="staff@example.com",
            password="Password123!",
            first_name="Staff",
            last_name="User",
        )
        RoleAssignment.objects.create(user=self.staff_user, role=self.role_staff)

        self.client.force_authenticate(user=self.owner_user)

    def test_01_venue_crud_and_location_creation(self):
        """Test Venue creation with nested/flat location, update, and soft delete."""
        create_payload = {
            "name": "Grand Medicare Auditorium",
            "description": "Premium multi-specialty convention hall",
            "capacity": 500,
            "price_per_event": "15000.00",
            "building_name": "Block C, Medicare Hub",
            "address_line1": "123 Health Way",
            "city": "Mumbai",
            "state": "Maharashtra",
            "postal_code": "400001",
            "location_type": "EVENT",
        }

        response = self.client.post("/api/catalog/venues/", create_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        venue_id = response.data["id"]
        venue = Venue.objects.get(id=venue_id)
        self.assertEqual(venue.name, "Grand Medicare Auditorium")
        self.assertIsNotNone(venue.location)
        self.assertEqual(venue.location.city, "Mumbai")
        self.assertEqual(venue.location.building_name, "Block C, Medicare Hub")

        # Test GET details
        get_response = self.client.get(f"/api/catalog/venues/{venue_id}/")
        self.assertEqual(get_response.status_code, status.HTTP_200_OK)
        self.assertEqual(get_response.data["location"]["city"], "Mumbai")

        # Test Update location
        update_payload = {
            "name": "Grand Medicare Convention Center",
            "city": "Pune",
            "capacity": 600,
        }
        patch_response = self.client.patch(f"/api/catalog/venues/{venue_id}/", update_payload, format="json")
        self.assertEqual(patch_response.status_code, status.HTTP_200_OK)
        venue.refresh_from_db()
        self.assertEqual(venue.name, "Grand Medicare Convention Center")
        self.assertEqual(venue.location.city, "Pune")
        self.assertEqual(venue.capacity, 600)

        # Test Dropdown endpoint
        dropdown_response = self.client.get("/api/catalog/venues/dropdown/")
        self.assertEqual(dropdown_response.status_code, status.HTTP_200_OK)
        self.assertTrue(any(v["id"] == venue_id for v in dropdown_response.data))

        # Test Soft Delete
        del_response = self.client.delete(f"/api/catalog/venues/{venue_id}/")
        self.assertEqual(del_response.status_code, status.HTTP_204_NO_CONTENT)
        venue.refresh_from_db()
        self.assertTrue(venue.is_deleted)
        self.assertFalse(venue.is_active)

    def test_02_service_crud_and_venue_association(self):
        """Test Service creation, many-to-many venue link, and soft delete."""
        venue = Venue.objects.create(
            owner=self.owner_user,
            name="Apollo Conference Center",
            capacity=200,
            price_per_event=Decimal("8000.00"),
        )

        service_payload = {
            "name": "Cardiology Consultation & Diagnostics",
            "service_type": BookingType.OPD,
            "description": "Specialized cardiac OPD session",
            "address": "Floor 2, Wing A",
            "city": "Mumbai",
            "contact": "9876543210",
            "venue": [venue.id],
        }

        response = self.client.post("/api/catalog/services/", service_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        service_id = response.data["id"]
        service = Service.objects.get(id=service_id)
        self.assertEqual(service.name, "Cardiology Consultation & Diagnostics")
        self.assertTrue(service.venue.filter(id=venue.id).exists())

        # Test Dropdown
        dropdown_res = self.client.get("/api/catalog/services/dropdown/")
        self.assertEqual(dropdown_res.status_code, status.HTTP_200_OK)
        self.assertTrue(any(s["id"] == service_id for s in dropdown_res.data))

        # Soft delete
        del_res = self.client.delete(f"/api/catalog/services/{service_id}/")
        self.assertEqual(del_res.status_code, status.HTTP_204_NO_CONTENT)
        service.refresh_from_db()
        self.assertTrue(service.is_deleted)
        self.assertFalse(service.is_active)

    def test_03_resource_crud_and_quantity_validation(self):
        """Test Resource creation, quantity validations, and pricing."""
        resource_payload = {
            "name": "High-Definition 4K Projector & Screen",
            "description": "AV equipment for auditoriums",
            "total_quantity": 5,
            "available_quantity": 5,
            "sell_price_per_unit": "45000.00",
            "rent_price_per_unit_per_day": "2500.00",
        }

        response = self.client.post("/api/catalog/resources/", resource_payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        res_id = response.data["id"]

        resource = Resource.objects.get(id=res_id)
        self.assertEqual(resource.available_quantity, 5)

        # Invalid quantity test: available > total
        invalid_payload = {
            "name": "Surround Sound Audio Kit",
            "total_quantity": 2,
            "available_quantity": 5,
        }
        err_res = self.client.post("/api/catalog/resources/", invalid_payload, format="json")
        self.assertEqual(err_res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("available_quantity", err_res.data)

        # Soft delete
        del_res = self.client.delete(f"/api/catalog/resources/{res_id}/")
        self.assertEqual(del_res.status_code, status.HTTP_204_NO_CONTENT)
        resource.refresh_from_db()
        self.assertTrue(resource.is_deleted)

    def test_04_photos_upload_lifecycle(self):
        """Test photo attachment via PhotoService and PhotoViewSet upload/primary/delete."""
        venue = Venue.objects.create(
            owner=self.owner_user,
            name="Zenith Banquet",
            capacity=300,
        )

        test_img = create_test_image("venue_photo_1.png", "green")
        photo, _ = PhotoService.attach_photo("venue", venue.id, test_img, is_primary=True)
        self.assertTrue(photo.is_primary)
        self.assertEqual(venue.photos.count(), 1)

        # Attach second photo
        test_img_2 = create_test_image("venue_photo_2.png", "red")
        photo_2, _ = PhotoService.attach_photo("venue", venue.id, test_img_2, is_primary=False)
        self.assertFalse(photo_2.is_primary)

        # Set photo_2 as primary via service
        PhotoService.set_primary(photo_2.id)
        photo.refresh_from_db()
        photo_2.refresh_from_db()
        self.assertFalse(photo.is_primary)
        self.assertTrue(photo_2.is_primary)

        # Test photo upload via REST endpoint
        upload_img = create_test_image("api_upload.png", "purple")
        upload_res = self.client.post(
            "/api/catalog/photos/upload/",
            {
                "entity_type": "venue",
                "object_id": venue.id,
                "image": upload_img,
                "is_primary": False,
            },
            format="multipart",
        )
        self.assertEqual(upload_res.status_code, status.HTTP_201_CREATED, upload_res.data)
        new_photo_id = upload_res.data["id"]

        # Delete photo via REST endpoint
        del_res = self.client.delete(f"/api/catalog/photos/{new_photo_id}/")
        self.assertEqual(del_res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Photos.objects.filter(id=new_photo_id).exists())

    def test_05_user_assignment_to_entities(self):
        """Test assigning and unassigning managers and staff via EntityAssignUsersAPI."""
        venue = Venue.objects.create(
            owner=self.owner_user,
            name="City Pavilion",
            capacity=400,
        )

        # Assign manager and staff
        assign_payload = {
            "entity_id": venue.id,
            "employee_ids": [self.manager_user.id, self.staff_user.id],
        }

        assign_res = self.client.post("/api/catalog/assign-users/venue/", assign_payload, format="json")
        self.assertEqual(assign_res.status_code, status.HTTP_200_OK, assign_res.data)

        venue.refresh_from_db()
        self.assertTrue(venue.manager.filter(id=self.manager_user.id).exists())
        self.assertTrue(venue.staff.filter(id=self.staff_user.id).exists())

        # Unassign staff
        unassign_payload = {
            "entity_id": venue.id,
            "employee_ids": [self.staff_user.id],
        }
        unassign_res = self.client.delete("/api/catalog/assign-users/venue/", unassign_payload, format="json")
        self.assertEqual(unassign_res.status_code, status.HTTP_200_OK, unassign_res.data)

        venue.refresh_from_db()
        self.assertTrue(venue.manager.filter(id=self.manager_user.id).exists())
        self.assertFalse(venue.staff.filter(id=self.staff_user.id).exists())

    def test_06_filtering_and_search(self):
        """Test filtering by city, capacity range, and search."""
        loc1 = Location.objects.create(city="Delhi", state="Delhi", postal_code="110001", location_type="OPD")
        loc2 = Location.objects.create(city="Mumbai", state="Maharashtra", postal_code="400001", location_type="OPD")

        v1 = Venue.objects.create(owner=self.owner_user, name="Delhi Central Hall", location=loc1, capacity=100, price_per_event=5000)
        v2 = Venue.objects.create(owner=self.owner_user, name="Mumbai Coastal Arena", location=loc2, capacity=500, price_per_event=25000)

        # Filter by city
        res = self.client.get("/api/catalog/venues/?city=Delhi")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data.get("results", res.data)
        ids = [item["id"] for item in results]
        self.assertIn(v1.id, ids)
        self.assertNotIn(v2.id, ids)

        # Filter by capacity range
        res_cap = self.client.get("/api/catalog/venues/?min_capacity=300")
        results_cap = res_cap.data.get("results", res_cap.data)
        ids_cap = [item["id"] for item in results_cap]
        self.assertIn(v2.id, ids_cap)
        self.assertNotIn(v1.id, ids_cap)

        # Search by keyword
        res_search = self.client.get("/api/catalog/venues/?search=Coastal")
        results_search = res_search.data.get("results", res_search.data)
        ids_search = [item["id"] for item in results_search]
        self.assertIn(v2.id, ids_search)
        self.assertNotIn(v1.id, ids_search)
