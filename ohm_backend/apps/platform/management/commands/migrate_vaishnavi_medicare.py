import re
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
import psycopg
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.accounts.models import (
    EmployeeProfile,
    ShiftSchedule,
    StaffForHire,
    User,
    UserDocument,
    UserDocumentFile,
)
from apps.billing.models import (
    Invoice,
    InvoiceItem,
    Location,
    Package,
    Patient,
    PatientDocument,
    PatientDocumentFile,
    Payment,
    PrimaryOrder,
    SecondaryOrder,
)
from apps.catalog.models import Photos, Service, Venue
from apps.content.models import Contact, FAQItem, FAQTopic
from apps.payroll.models import (
    Attendance,
    AttendanceStatus,
    SalaryReport,
    SalaryStructure,
    SalaryTransaction,
)
from apps.platform.models import Tenant
from apps.wallet.models import Wallet


class Command(BaseCommand):
    help = "Migrate data from legacy Vaishnavi Medicare staging database into the O-HM tenant schema."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default="vaishnavi_medicare",
            help="Target tenant schema name (default: vaishnavi_medicare)",
        )
        parser.add_argument(
            "--source-db",
            default="vaishnavi_staging",
            help="Source PostgreSQL database name (default: vaishnavi_staging)",
        )
        parser.add_argument(
            "--source-host",
            default="localhost",
            help="Source PostgreSQL host (default: localhost)",
        )
        parser.add_argument(
            "--source-port",
            default=5432,
            type=int,
            help="Source PostgreSQL port (default: 5432)",
        )
        parser.add_argument(
            "--source-user",
            default="postgres",
            help="Source PostgreSQL user (default: postgres)",
        )
        parser.add_argument(
            "--source-password",
            default="postgres",
            help="Source PostgreSQL password (default: postgres)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Perform transfer in a rolled-back transaction for verification",
        )

    def handle(self, *args, **options):
        tenant_schema = options["tenant"]
        source_db = options["source_db"]
        dry_run = options["dry_run"]

        self.stdout.write(
            self.style.NOTICE(
                f"=== Starting Vaishnavi Medicare Migration ===\n"
                f"Source: {source_db}@{options['source_host']}:{options['source_port']}\n"
                f"Target Schema: {tenant_schema}\n"
                f"Dry-run: {dry_run}\n"
            )
        )

        tenant = Tenant.objects.filter(schema_name=tenant_schema).first()
        if not tenant:
            self.stderr.write(self.style.ERROR(f"Tenant schema '{tenant_schema}' not found. Please provision it first."))
            return

        conn = psycopg.connect(
            dbname=source_db,
            user=options["source_user"],
            password=options["source_password"],
            host=options["source_host"],
            port=options["source_port"],
        )
        cur = conn.cursor()

        try:
            with schema_context(tenant_schema):
                with transaction.atomic():
                    self._run_migration(cur)

                    if dry_run:
                        self.stdout.write(self.style.WARNING("\n[DRY RUN] Rolling back all changes."))
                        transaction.set_rollback(True)
                    else:
                        self.stdout.write(self.style.SUCCESS("\n[SUCCESS] Migration committed successfully!"))
        finally:
            cur.close()
            conn.close()

    def _run_migration(self, cur):
        id_maps = {
            "users": {},
            "shifts": {},
            "locations": {},
            "venues": {},
            "services": {},
            "patients": {},
            "patient_docs": {},
            "packages": {},
            "primary_orders": {},
            "secondary_orders": {},
            "invoices": {},
            "salary_reports": {},
            "attendance_statuses": {},
            "faq_topics": {},
            "user_docs": {},
        }

        # 1. ShiftSchedules
        self._migrate_shift_schedules(cur, id_maps)

        # 2. Users
        self._migrate_users(cur, id_maps)

        # 3. EmployeeProfiles
        self._migrate_employee_profiles(cur, id_maps)

        # 4. StaffForHire
        self._migrate_staff_for_hire(cur, id_maps)

        # 5. User Documents
        self._migrate_user_documents(cur, id_maps)

        # 6. Locations
        self._migrate_locations(cur, id_maps)

        # 7. Venues & Services
        self._migrate_venues_and_services(cur, id_maps)

        # 8. Photos
        self._migrate_photos(cur, id_maps)

        # 9. Patients & Patient Documents
        self._migrate_patients(cur, id_maps)

        # 10. Packages
        self._migrate_packages(cur, id_maps)

        # 11. Orders
        self._migrate_orders(cur, id_maps)

        # 12. Invoices & Payments
        self._migrate_invoices_and_payments(cur, id_maps)

        # 13. Wallets
        self._migrate_wallets(cur, id_maps)

        # 14. Payroll & Attendance
        self._migrate_payroll(cur, id_maps)

        # 15. Content (FAQs & Contacts)
        self._migrate_content(cur, id_maps)

    def _migrate_shift_schedules(self, cur, id_maps):
        self.stdout.write("Migrating ShiftSchedules...")
        cur.execute("SELECT id, name, start_time, end_time, is_overnight, weekly_off_days, is_active, grace_minutes FROM accounts_shiftschedule;")
        rows = cur.fetchall()
        for r in rows:
            obj, _ = ShiftSchedule.objects.get_or_create(
                name=r[1],
                defaults={
                    "start_time": r[2],
                    "end_time": r[3],
                    "is_overnight": r[4],
                    "weekly_off_days": r[5] or [0],
                    "is_active": r[6],
                    "grace_minutes": r[7] or 0,
                },
            )
            id_maps["shifts"][r[0]] = obj.id
        self.stdout.write(f"  Migrated {len(id_maps['shifts'])} ShiftSchedules.")

    def _migrate_users(self, cur, id_maps):
        self.stdout.write("Migrating Users...")
        cur.execute("""
            SELECT id, password, last_login, is_superuser, email, mobile_number, first_name,
                   middle_name, last_name, gender, city, is_active, is_staff, date_joined,
                   created_by_id, is_deleted, age, alternate_phone_number, emergency_contact_name,
                   emergency_contact_number, current_address, permanent_address
            FROM accounts_customuser
            ORDER BY id ASC;
        """)
        rows = cur.fetchall()
        created_count = 0
        existing_emails = set(User.objects.values_list("email", flat=True))

        for r in rows:
            src_id = r[0]
            email = (r[4] or "").strip().lower()
            mobile = re.sub(r"[^\d]", "", r[5] or "") if r[5] else None

            # Fallback for null/empty email to satisfy unique non-null constraint
            if not email:
                email = f"user_{src_id}@vaishnavimedicare.internal"

            date_joined = r[13]
            if date_joined:
                if isinstance(date_joined, datetime):
                    if timezone.is_naive(date_joined):
                        date_joined = timezone.make_aware(date_joined)
                else:
                    date_joined = timezone.make_aware(datetime.combine(date_joined, datetime.min.time()))
            else:
                date_joined = timezone.now()

            if email in existing_emails:
                user = User.objects.get(email=email)
            else:
                user = User(
                    password=r[1],
                    last_login=r[2],
                    is_superuser=r[3],
                    email=email,
                    mobile_number=mobile or None,
                    first_name=r[6] or "",
                    middle_name=r[7] or "",
                    last_name=r[8] or "",
                    gender=r[9] if r[9] in ["M", "F", "O", "N"] else "",
                    city=r[10] or "",
                    is_active=r[11],
                    is_staff=r[12],
                    date_joined=date_joined,
                    is_deleted=r[15],
                    age=r[16],
                    alternate_phone_number=r[17] or "",
                    emergency_contact_name=r[18] or "",
                    emergency_contact_number=r[19] or "",
                    current_address=r[20] or "",
                    permanent_address=r[21] or "",
                )
                user.save()
                existing_emails.add(email)
                created_count += 1

            id_maps["users"][src_id] = user.id

        # Second pass: wire created_by FK
        for r in rows:
            src_id = r[0]
            created_by_src = r[14]
            if created_by_src and created_by_src in id_maps["users"]:
                target_user_id = id_maps["users"][src_id]
                User.objects.filter(id=target_user_id).update(created_by_id=id_maps["users"][created_by_src])

        self.stdout.write(f"  Migrated {len(id_maps['users'])} Users ({created_count} created).")

    def _migrate_employee_profiles(self, cur, id_maps):
        self.stdout.write("Migrating EmployeeProfiles...")
        cur.execute("""
            SELECT id, employee_id, category, vendor_name, vendor_phone, last_working_day,
                   skills, target_percent, qc_required, pf_applicable, pf_number, uan_number,
                   esi_applicable, esi_number, esi_dispensary, shift_effective_from, user_id,
                   shift_id, cost_center, department, designation, grade, rehired_status,
                   termination_reason, termination_type
            FROM accounts_employeeprofile;
        """)
        rows = cur.fetchall()
        count = 0
        for r in rows:
            target_user_id = id_maps["users"].get(r[16])
            if not target_user_id:
                continue

            target_shift_id = id_maps["shifts"].get(r[17])
            category = r[2] if r[2] in ["REGULAR", "FULLTIME", "PARTTIME", "VIRTUAL", "PPO", "VENDOR"] else None
            termination_type = r[24] if r[24] in ["VOLUNTARY", "INVOLUNTARY"] else None
            rehired_status = r[22] if r[22] in ["YES", "NO"] else "NO"

            EmployeeProfile.objects.update_or_create(
                user_id=target_user_id,
                defaults={
                    "employee_id": r[1],
                    "category": category,
                    "vendor_name": r[3],
                    "vendor_phone": r[4],
                    "last_working_day": r[5],
                    "skills": r[6],
                    "pf_applicable": r[9],
                    "pf_number": r[10],
                    "uan_number": r[11],
                    "esi_applicable": r[12],
                    "esi_number": r[13],
                    "esi_dispensary": r[14],
                    "shift_effective_from": r[15],
                    "shift_id": target_shift_id,
                    "cost_center": r[18],
                    "department": r[19],
                    "designation": r[20],
                    "grade": r[21],
                    "rehired_status": rehired_status,
                    "termination_reason": r[23],
                    "termination_type": termination_type,
                },
            )
            count += 1
        self.stdout.write(f"  Migrated {count} EmployeeProfiles.")

    def _migrate_staff_for_hire(self, cur, id_maps):
        self.stdout.write("Migrating StaffForHire...")
        cur.execute("""
            SELECT id, staff_name, email, mobile_number, vendor_name, available_from,
                   available_for, language, skill, price, is_active, is_available, created_by_id
            FROM accounts_staffforhire;
        """)
        rows = cur.fetchall()
        for r in rows:
            created_by_id = id_maps["users"].get(r[12])
            StaffForHire.objects.get_or_create(
                staff_name=r[1],
                email=r[2],
                mobile_number=r[3],
                defaults={
                    "vendor_name": r[4],
                    "available_from": r[5],
                    "available_for": r[6],
                    "language": r[7],
                    "skill": r[8],
                    "price": r[9],
                    "is_active": r[10],
                    "is_available": r[11],
                    "created_by_id": created_by_id,
                },
            )
        self.stdout.write(f"  Migrated {len(rows)} StaffForHire.")

    def _migrate_user_documents(self, cur, id_maps):
        self.stdout.write("Migrating UserDocuments...")
        cur.execute("SELECT id, title, remarks, user_id, uploaded_by_id, created_at, updated_at FROM accounts_userdocument;")
        docs = cur.fetchall()
        for r in docs:
            user_id = id_maps["users"].get(r[3])
            if not user_id:
                continue
            uploaded_by_id = id_maps["users"].get(r[4])
            doc = UserDocument.objects.create(
                title=r[1] or "",
                remarks=r[2] or "",
                user_id=user_id,
                uploaded_by_id=uploaded_by_id,
                created_at=r[5],
                updated_at=r[6],
            )
            id_maps["user_docs"][r[0]] = doc.id

        cur.execute("SELECT id, file, uploaded_at, document_id FROM accounts_userdocumentfile;")
        files = cur.fetchall()
        for r in files:
            doc_id = id_maps["user_docs"].get(r[3])
            if not doc_id:
                continue
            UserDocumentFile.objects.create(
                document_id=doc_id,
                file=r[1],
                uploaded_at=r[2],
            )
        self.stdout.write(f"  Migrated {len(docs)} UserDocuments and {len(files)} DocumentFiles.")

    def _migrate_locations(self, cur, id_maps):
        self.stdout.write("Migrating Locations...")
        cur.execute("""
            SELECT id, location_type, building_name, address_line1, address_line2,
                   locality, city, state, postal_code, user_id
            FROM booking_location;
        """)
        rows = cur.fetchall()
        for r in rows:
            user_id = id_maps["users"].get(r[9])
            loc = Location.objects.create(
                location_type=r[1],
                building_name=r[2],
                address_line1=r[3],
                address_line2=r[4],
                locality=r[5],
                city=r[6],
                state=r[7],
                postal_code=r[8],
                user_id=user_id,
            )
            id_maps["locations"][r[0]] = loc.id
        self.stdout.write(f"  Migrated {len(id_maps['locations'])} Locations.")

    def _migrate_venues_and_services(self, cur, id_maps):
        self.stdout.write("Migrating Venues & Services...")
        # Venues
        cur.execute("""
            SELECT id, name, description, contact, website, social_links, capacity,
                   price_per_event, rooms, floors, parking_slots, external_decorators_allow,
                   external_caterers_allow, amenities, seating_arrangement, is_active,
                   is_deleted, created_at, updated_at, owner_id, location_id
            FROM venue_manager_venue;
        """)
        venues = cur.fetchall()
        for r in venues:
            owner_id = id_maps["users"].get(r[19])
            location_id = id_maps["locations"].get(r[20])
            venue = Venue.objects.create(
                name=r[1],
                description=r[2] or "",
                contact=r[3],
                website=r[4],
                social_links=r[5],
                capacity=r[6] or 0,
                price_per_event=r[7],
                rooms=r[8] or 0,
                floors=r[9] or 0,
                parking_slots=r[10] or {},
                external_decorators_allow=r[11],
                external_caterers_allow=r[12],
                amenities=r[13] or [],
                seating_arrangement=r[14] or [],
                is_active=r[15],
                is_deleted=r[16],
                created_at=r[17],
                updated_at=r[18],
                owner_id=owner_id,
                location_id=location_id,
            )
            id_maps["venues"][r[0]] = venue.id

        # Services
        cur.execute("""
            SELECT id, name, description, address, website, tags, quick_info, is_active,
                   created_at, updated_at, owner_id, city, is_deleted, contact, service_type
            FROM venue_manager_service;
        """)
        services = cur.fetchall()
        for r in services:
            owner_id = id_maps["users"].get(r[10])
            svc = Service.objects.create(
                name=r[1],
                description=r[2] or "",
                address=r[3] or "",
                website=r[4],
                tags=r[5] or [],
                quick_info=r[6] or {},
                is_active=r[7],
                created_at=r[8],
                updated_at=r[9],
                owner_id=owner_id,
                city=r[11] or "",
                is_deleted=r[12],
                contact=r[13],
                service_type=r[14] or "OPD",
            )
            id_maps["services"][r[0]] = svc.id

        # Service-Venue M2M
        cur.execute("SELECT service_id, venue_id FROM venue_manager_service_venue;")
        m2m_rows = cur.fetchall()
        for s_id, v_id in m2m_rows:
            target_s_id = id_maps["services"].get(s_id)
            target_v_id = id_maps["venues"].get(v_id)
            if target_s_id and target_v_id:
                svc = Service.objects.get(id=target_s_id)
                svc.venue.add(target_v_id)

        self.stdout.write(f"  Migrated {len(id_maps['venues'])} Venues and {len(id_maps['services'])} Services.")

    def _migrate_photos(self, cur, id_maps):
        self.stdout.write("Migrating Entity Photos...")
        cur.execute("""
            SELECT p.id, p.image, p.is_primary, p.uploaded_at, p.object_id, ct.app_label, ct.model
            FROM venue_manager_photos p
            JOIN django_content_type ct ON p.content_type_id = ct.id;
        """)
        photos = cur.fetchall()
        venue_ct = ContentType.objects.get(app_label="catalog", model="venue")
        service_ct = ContentType.objects.get(app_label="catalog", model="service")

        count = 0
        for r in photos:
            legacy_app, legacy_model, legacy_obj_id = r[5], r[6], r[4]
            target_ct = None
            target_obj_id = None

            if legacy_app == "venue_manager" and legacy_model == "venue":
                target_ct = venue_ct
                target_obj_id = id_maps["venues"].get(legacy_obj_id)
            elif legacy_app == "venue_manager" and legacy_model == "service":
                target_ct = service_ct
                target_obj_id = id_maps["services"].get(legacy_obj_id)

            if target_ct and target_obj_id:
                Photos.objects.create(
                    image=r[1],
                    is_primary=r[2],
                    uploaded_at=r[3],
                    content_type=target_ct,
                    object_id=target_obj_id,
                )
                count += 1
        self.stdout.write(f"  Migrated {count} Photos.")

    def _migrate_patients(self, cur, id_maps):
        self.stdout.write("Migrating Patients & Clinical Documents...")
        cur.execute("""
            SELECT id, first_name, last_name, email, phone, address, age, emergency_contact,
                   emergency_phone, emergency_contact_2, emergency_phone_2, medical_conditions,
                   allergies, present_health_condition, gender, blood_group, preferred_language,
                   education_qualifications, earlier_occupation, year_of_retirement, affiliate,
                   source, referred_by, is_probono, is_registration_fees_paid, is_deleted,
                   is_active, registration_date, updated_at, registered_by_id, patient_id
            FROM booking_patient;
        """)
        patients = cur.fetchall()

        valid_genders = {"male": "male", "female": "female", "other": "other", "prefer-not-to-say": "prefer-not-to-say"}
        gender_lookup = {"m": "male", "f": "female", "male": "male", "female": "female", "other": "other"}
        valid_blood = {"A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"}

        for r in patients:
            reg_by = id_maps["users"].get(r[29])
            g_raw = (r[14] or "").strip().lower()
            gender = gender_lookup.get(g_raw, "prefer-not-to-say")

            bg_raw = (r[15] or "").strip().upper()
            if bg_raw == "AB+":
                blood_group = "AB+"
            else:
                blood_group = bg_raw if bg_raw in valid_blood else None

            first_name = (r[1] or "").strip()
            last_name = (r[2] or "").strip() or "-"
            phone = re.sub(r"[^\d]", "", r[4] or "") or "9999999999"
            em_contact = (r[7] or "").strip() or first_name or "Self"
            em_phone = re.sub(r"[^\d]", "", r[8] or "") or phone

            patient = Patient.objects.create(
                patient_id=r[30] or f"PAT-{r[0]:05d}",
                registered_by_id=reg_by,
                first_name=first_name,
                last_name=last_name,
                email=r[3],
                phone=phone,
                address=r[5] or "",
                age=r[6],
                emergency_contact=em_contact,
                emergency_phone=em_phone,
                emergency_contact_2=r[9],
                emergency_phone_2=r[10],
                medical_conditions=r[11],
                allergies=r[12],
                present_health_condition=r[13],
                gender=gender,
                blood_group=blood_group,
                preferred_language=r[16],
                education_qualifications=r[17],
                earlier_occupation=r[18],
                year_of_retirement=r[19],
                affiliate=r[20],
                source=r[21],
                referred_by=r[22],
                is_probono=r[23],
                is_registration_fees_paid=r[24],
                is_deleted=r[25],
                is_active=r[26],
                registration_date=r[27] or timezone.now(),
                updated_at=r[28] or timezone.now(),
            )
            id_maps["patients"][r[0]] = patient.id

        # Documents
        cur.execute("SELECT id, title, remarks, created_at, updated_at, patient_id, uploaded_by_id FROM booking_patientdocument;")
        docs = cur.fetchall()
        for r in docs:
            pat_id = id_maps["patients"].get(r[5])
            if not pat_id:
                continue
            up_by = id_maps["users"].get(r[6])
            doc = PatientDocument.objects.create(
                patient_id=pat_id,
                title=r[1],
                remarks=r[2],
                created_at=r[3],
                updated_at=r[4],
                uploaded_by_id=up_by,
            )
            id_maps["patient_docs"][r[0]] = doc.id

        cur.execute("SELECT id, file, uploaded_at, document_id FROM booking_patientdocumentfile;")
        files = cur.fetchall()
        for r in files:
            doc_id = id_maps["patient_docs"].get(r[3])
            if not doc_id:
                continue
            PatientDocumentFile.objects.create(
                document_id=doc_id,
                file=r[1],
                uploaded_at=r[2],
            )
        self.stdout.write(f"  Migrated {len(id_maps['patients'])} Patients, {len(docs)} Documents, {len(files)} Files.")

    def _migrate_packages(self, cur, id_maps):
        self.stdout.write("Migrating Packages...")
        cur.execute("""
            SELECT p.id, p.name, p.description, p.package_type, p.price, p.is_active,
                   p.created_at, p.updated_at, p.owner_id, p.period, p.registration_fees,
                   p.object_id, ct.app_label, ct.model
            FROM booking_package p
            JOIN django_content_type ct ON p.content_type_id = ct.id;
        """)
        packages = cur.fetchall()
        venue_ct = ContentType.objects.get(app_label="catalog", model="venue")
        service_ct = ContentType.objects.get(app_label="catalog", model="service")

        for r in packages:
            owner_id = id_maps["users"].get(r[8])
            legacy_app, legacy_model, legacy_obj = r[12], r[13], r[11]
            target_ct = None
            target_obj = None

            if legacy_app == "venue_manager" and legacy_model == "venue":
                target_ct = venue_ct
                target_obj = id_maps["venues"].get(legacy_obj)
            elif legacy_app == "venue_manager" and legacy_model == "service":
                target_ct = service_ct
                target_obj = id_maps["services"].get(legacy_obj)

            pkg = Package.objects.create(
                owner_id=owner_id,
                content_type=target_ct,
                object_id=target_obj,
                name=r[1],
                description=r[2],
                package_type=r[3],
                price=r[4],
                is_active=r[5],
                created_at=r[6],
                updated_at=r[7],
                period=r[9] or "MONTHLY",
                registration_fees=r[10] or Decimal("0.00"),
            )
            id_maps["packages"][r[0]] = pkg.id
        self.stdout.write(f"  Migrated {len(id_maps['packages'])} Packages.")

    def _migrate_orders(self, cur, id_maps):
        self.stdout.write("Migrating Orders...")
        # Primary Orders
        cur.execute("""
            SELECT id, order_id, booking_entity, start_datetime, end_datetime, total_bill,
                   booking_type, status, auto_continue, created_at, updated_at, package_id,
                   patient_id, service_id, user_id, venue_id, discount_amount, premium_amount,
                   status_locked, client_address, raw_dates
            FROM booking_primaryorder;
        """)
        primary_orders = cur.fetchall()
        for r in primary_orders:
            user_id = id_maps["users"].get(r[14])
            patient_id = id_maps["patients"].get(r[12])
            if not user_id or not patient_id:
                continue

            order = PrimaryOrder.objects.create(
                order_id=r[1] or f"#ORD-{r[0]:06d}",
                booking_entity=r[2] or "SERVICE",
                user_id=user_id,
                patient_id=patient_id,
                start_datetime=r[3],
                end_datetime=r[4],
                total_bill=r[5] or Decimal("0.00"),
                booking_type=r[6] or "IN_HOUSE",
                status=r[7] or "LOBBY",
                auto_continue=r[8],
                created_at=r[9],
                updated_at=r[10],
                discount_amount=r[16] or Decimal("0.00"),
                premium_amount=r[17] or Decimal("0.00"),
                status_locked=r[18],
                notes=r[19] or "",
                raw_dates=r[20] or {},
            )
            id_maps["primary_orders"][r[0]] = order.id

        # Secondary Orders
        cur.execute("""
            SELECT id, order_id, start_datetime, end_datetime, subtotal, status,
                   created_at, updated_at, primary_order_id, status_locked, is_registration_fee
            FROM booking_secondaryorder;
        """)
        sec_orders = cur.fetchall()
        for r in sec_orders:
            p_order_id = id_maps["primary_orders"].get(r[8])
            if not p_order_id:
                continue
            parent = PrimaryOrder.objects.get(id=p_order_id)

            sec_order = SecondaryOrder.objects.create(
                primary_order=parent,
                order_id=r[1] or f"#SEC-{r[0]:07d}",
                start_datetime=r[2],
                end_datetime=r[3],
                subtotal=r[4] or Decimal("0.00"),
                status=r[5] or "LOBBY",
                created_at=r[6],
                updated_at=r[7],
                status_locked=r[9],
                is_registration_fee=r[10],
                booking_entity=parent.booking_entity,
                booking_type=parent.booking_type,
            )
            id_maps["secondary_orders"][r[0]] = sec_order.id

        self.stdout.write(f"  Migrated {len(id_maps['primary_orders'])} PrimaryOrders and {len(id_maps['secondary_orders'])} SecondaryOrders.")

    def _migrate_invoices_and_payments(self, cur, id_maps):
        self.stdout.write("Migrating Invoices and Payments...")
        cur.execute("""
            SELECT id, total_amount, paid_amount, remaining_amount, status, due_date,
                   created_at, updated_at, patient_id, user_id, invoice_number, issued_date,
                   tax_amount, period_end, period_start, subtotal, secondary_order_id,
                   discount_amount, parent_invoice_id, premium_amount, ternary_order_id
            FROM booking_totalinvoice;
        """)
        invoices = cur.fetchall()

        for r in invoices:
            pat_id = id_maps["patients"].get(r[8])
            usr_id = id_maps["users"].get(r[9])
            sec_id = id_maps["secondary_orders"].get(r[16])

            subtotal = r[15] or Decimal("0.00")
            discount = r[17] or Decimal("0.00")
            premium = r[19] or Decimal("0.00")
            taxable = subtotal - discount + premium

            inv = Invoice.objects.create(
                invoice_number=r[10] or f"INV-{r[0]:08d}",
                status=r[4] or "UNPAID",
                invoice_type_tag="BOOKING_SECONDARY",
                patient_id=pat_id,
                user_id=usr_id,
                secondary_order_id=sec_id,
                total_amount=r[1] or Decimal("0.00"),
                paid_amount=r[2] or Decimal("0.00"),
                remaining_amount=r[3] or Decimal("0.00"),
                due_date=r[5],
                created_at=r[6],
                updated_at=r[7],
                issue_date=r[11] or timezone.localdate(),
                tax_amount=r[12] or Decimal("0.00"),
                period_end=r[13],
                period_start=r[14],
                subtotal=subtotal,
                discount_amount=discount,
                premium_amount=premium,
                taxable_amount=taxable,
                currency="INR",
                state="Karnataka",
            )
            id_maps["invoices"][r[0]] = inv.id

            # Synthesize 1 InvoiceItem to reconcile line totals
            InvoiceItem.objects.create(
                invoice=inv,
                position=1,
                line_type="catalog",
                description=f"Care Service - {inv.invoice_number}",
                hsn_sac="9993",
                quantity=Decimal("1.00"),
                uom="OTH",
                unit_price=subtotal,
                discount_amount=discount,
                taxable_value=taxable,
                amount=subtotal,
                gst_rate=Decimal("0.00"),
            )

        # Payments
        cur.execute("""
            SELECT id, amount, method, reference, created_at, patient_id, invoice_id,
                   is_verified, paid_date, updated_at, mapping_status, mapping_meta
            FROM booking_payment;
        """)
        payments = cur.fetchall()

        valid_methods = {"CASH", "UPI", "CARD", "BANK", "CHEQUE", "RAZORPAY", "WALLET", "WALLET_REFUND"}
        for r in payments:
            pat_id = id_maps["patients"].get(r[5])
            inv_id = id_maps["invoices"].get(r[6])
            method = r[2] if r[2] in valid_methods else "CASH"

            Payment.objects.create(
                amount=r[1] or Decimal("0.00"),
                method=method,
                reference=r[3] or "",
                created_at=r[4],
                patient_id=pat_id,
                invoice_id=inv_id,
                is_verified=r[7],
                paid_date=r[8] or timezone.now(),
                updated_at=r[9],
                mapping_status=r[10] or "UNMAPPED",
                mapping_meta=r[11] or {},
                currency="INR",
            )
        self.stdout.write(f"  Migrated {len(id_maps['invoices'])} Invoices and {len(payments)} Payments.")

    def _migrate_wallets(self, cur, id_maps):
        self.stdout.write("Migrating Wallets...")
        cur.execute("SELECT id, balance, last_updated, created_at, updated_at, user_id FROM wallet_wallet;")
        rows = cur.fetchall()
        for r in rows:
            user_id = id_maps["users"].get(r[5])
            if not user_id:
                continue
            Wallet.objects.update_or_create(
                user_id=user_id,
                defaults={
                    "balance": r[1] or Decimal("0.00"),
                    "last_updated": r[2] or timezone.now(),
                    "created_at": r[3],
                    "updated_at": r[4],
                },
            )
        self.stdout.write(f"  Migrated {len(rows)} Wallets.")

    def _migrate_payroll(self, cur, id_maps):
        self.stdout.write("Migrating Payroll & Attendance...")
        # Attendance Statuses
        cur.execute("SELECT id, code, label, is_active FROM payroll_attendancestatus;")
        statuses = cur.fetchall()
        for r in statuses:
            st, _ = AttendanceStatus.objects.get_or_create(
                code=r[1],
                defaults={"label": r[2], "is_active": r[3]},
            )
            id_maps["attendance_statuses"][r[0]] = st.id

        # Attendance (in bulk)
        cur.execute("SELECT id, date, duration, reason, user_id, status_id FROM payroll_attendance;")
        att_rows = cur.fetchall()
        att_objs = []
        for r in att_rows:
            user_id = id_maps["users"].get(r[4])
            st_id = id_maps["attendance_statuses"].get(r[5])
            if user_id and st_id:
                att_objs.append(
                    Attendance(
                        user_id=user_id,
                        status_id=st_id,
                        date=r[1],
                        duration=r[2],
                        reason=r[3] or "",
                    )
                )
        if att_objs:
            Attendance.objects.bulk_create(att_objs, batch_size=2000, ignore_conflicts=True)
        self.stdout.write(f"  Migrated {len(att_objs)} Attendance records.")

        # Salary Structures
        cur.execute("""
            SELECT id, salary_type, final_salary, user_id, effective_from, amount,
                   change_type, esi_amount, pf_amount, salary_mode
            FROM payroll_salarystructure;
        """)
        structs = cur.fetchall()
        for r in structs:
            user_id = id_maps["users"].get(r[3])
            if not user_id:
                continue
            SalaryStructure.objects.create(
                user_id=user_id,
                salary_type=r[1] or "MONTHLY",
                final_salary=r[2] or Decimal("0.00"),
                effective_from=r[4],
                amount=r[5] or Decimal("0.00"),
                change_type=r[6] or "BASE_SALARY",
                esi_amount=r[7] or Decimal("0.00"),
                pf_amount=r[8] or Decimal("0.00"),
                salary_mode=r[9] or "BANK_TRANSFER",
            )

        # Salary Reports
        cur.execute("""
            SELECT id, start_date, end_date, daily_rate, total_payable_amount, paid_amount,
                   remaining_payment, user_id, advance_amount, final_salary, absent_days,
                   half_days, is_finalized, paid_leave_days, payable_days, present_days,
                   unpaid_leave_days, created_at, updated_at
            FROM payroll_salaryreport;
        """)
        reports = cur.fetchall()
        for r in reports:
            user_id = id_maps["users"].get(r[7])
            if not user_id:
                continue
            rep = SalaryReport.objects.create(
                user_id=user_id,
                start_date=r[1],
                end_date=r[2],
                daily_rate=r[3] or Decimal("0.00"),
                total_payable_amount=r[4] or Decimal("0.00"),
                paid_amount=r[5] or Decimal("0.00"),
                remaining_payment=r[6] or Decimal("0.00"),
                advance_amount=r[8] or Decimal("0.00"),
                final_salary=r[9] or Decimal("0.00"),
                absent_days=r[10] or 0,
                half_days=r[11] or 0,
                is_finalized=r[12],
                paid_leave_days=r[13] or 0,
                payable_days=r[14] or Decimal("0.00"),
                present_days=r[15] or 0,
                unpaid_leave_days=r[16] or 0,
                created_at=r[17],
                updated_at=r[18],
            )
            id_maps["salary_reports"][r[0]] = rep.id

        # Salary Transactions
        cur.execute("""
            SELECT id, transaction_id, amount_paid, payment_method, payment_reference,
                   status, processed_at, note, salary_report_id, paid_at, user_id,
                   created_at, updated_at
            FROM payroll_salarytransaction;
        """)
        txns = cur.fetchall()
        valid_methods = {"BANK_TRANSFER", "UPI", "CASH", "CHECK", "OTHER"}
        for r in txns:
            user_id = id_maps["users"].get(r[10])
            rep_id = id_maps["salary_reports"].get(r[8])
            if not user_id or not rep_id:
                continue
            method = r[3] if r[3] in valid_methods else "BANK_TRANSFER"
            SalaryTransaction.objects.create(
                transaction_id=r[1] or f"TXN-{r[0]}",
                amount_paid=r[2] or Decimal("0.00"),
                payment_method=method,
                payment_reference=r[4],
                status=r[5] or "SUCCESS",
                processed_at=r[6],
                note=r[7],
                salary_report_id=rep_id,
                paid_at=r[9],
                user_id=user_id,
                created_at=r[11],
                updated_at=r[12],
            )
        self.stdout.write(f"  Migrated {len(structs)} SalaryStructures, {len(id_maps['salary_reports'])} Reports, {len(txns)} Transactions.")

    def _migrate_content(self, cur, id_maps):
        self.stdout.write("Migrating FAQs & Contacts...")
        cur.execute("SELECT id, topic, created_at, updated_at FROM faq_faqtopic;")
        topics = cur.fetchall()
        for r in topics:
            top = FAQTopic.objects.create(topic=r[1], created_at=r[2], updated_at=r[3])
            id_maps["faq_topics"][r[0]] = top.id

        cur.execute("SELECT id, question, answer, created_at, updated_at, topic_id FROM faq_faqitem;")
        items = cur.fetchall()
        for r in items:
            top_id = id_maps["faq_topics"].get(r[5])
            if not top_id:
                continue
            FAQItem.objects.create(topic_id=top_id, question=r[1], answer=r[2], created_at=r[3], updated_at=r[4])

        cur.execute("SELECT id, display_name, mobile_number, email, platform, url, created_at, updated_at FROM faq_contact;")
        contacts = cur.fetchall()
        valid_platforms = {"facebook", "twitter", "instagram", "linkedin", "youtube", "reddit", "quora", "website", "other"}
        for r in contacts:
            platform = r[4] if r[4] in valid_platforms else "other"
            Contact.objects.create(
                display_name=r[1],
                mobile_number=r[2],
                email=r[3],
                platform=platform,
                url=r[4],
                created_at=r[6],
                updated_at=r[7],
            )
        self.stdout.write(f"  Migrated {len(topics)} FAQTopics, {len(items)} FAQItems, {len(contacts)} Contacts.")
