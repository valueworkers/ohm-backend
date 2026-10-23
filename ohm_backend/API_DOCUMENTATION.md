# OHM Backend API Documentation

Complete HTTP API reference for the OHM Multi-Tenant Healthcare Platform.

All endpoints are hosted on the unified API host (`api.ohm.com` in production, `api.ohm.localhost:8000` or `localhost:8000` in development).

> [!NOTE]
> Postman collection and environment files are packaged in [`OHM Backend API.zip`](OHM%20Backend%20API.zip). You can import them directly into Postman.
>
> The collection is grouped by access context: **Public / Shared**, **Root / Super Admin** (public schema, no `X-Tenant`), **Tenant Workspace** (`X-Tenant: {{tenantSlug}}`), and **Tenant Owner Portal** (owner account in the public schema, no `X-Tenant`). Root and tenant login requests save tokens to separate environment variables so they are not mixed up.

---

## Global Request Headers

| Header | Description | Required On |
| :--- | :--- | :--- |
| `Content-Type` | `application/json` (or `multipart/form-data` for file uploads) | All mutation requests (`POST`, `PUT`, `PATCH`) |
| `Authorization` | `Bearer <access_token>` | All endpoints requiring authentication |
| `X-Tenant` | Slug of the tenant workspace (e.g., `city-hospital`) | **All tenant-scoped endpoints** |
| `X-Onboarding-Token`| Token returned upon onboarding application creation | Applicant requests for onboarding inspection / edits |

---

## Standard Response & Error Format

### Success Responses
- **200 OK**: Request completed successfully; returns requested object or array.
- **201 Created**: Resource created successfully; returns created object.
- **202 Accepted**: Request accepted for asynchronous background processing (e.g., tenant provisioning).
- **204 No Content**: Resource successfully deleted.

### Error Responses
Field validation errors return an object detailing each failed field:
```json
{
  "email": ["This email is already registered."],
  "password": ["This password is too common."]
}
```
Authentication or permission errors return:
```json
{
  "detail": "Authentication credentials were not provided."
}
```
Business invariant or conflict errors return:
```json
{
  "error": "A workspace must keep at least one active Tenant Admin."
}
```

---

## 1. Health & Public Catalogue

### 1.1 Health Check
- **Endpoint**: `GET /health/`
- **Authentication**: None (Public)
- **Headers**: Optional `X-Tenant` to inspect schema switching
- **Response `200 OK`**:
```json
{
  "status": "ok",
  "schema": "public"
}
```

### 1.2 List Active Plans
- **Endpoint**: `GET /api/platform/plans/`
- **Authentication**: None (Public). Returns active plans for public users; returns all plans for Super Admins.
- **Response `200 OK`**:
```json
[
  {
    "id": 1,
    "name": "Standard Clinic Plan",
    "code": "standard",
    "description": "Essential booking and management features",
    "price_per_month": "99.00",
    "currency": "USD",
    "is_active": true,
    "features": [
      {
        "code": "booking",
        "name": "Online Appointments",
        "description": "Patient appointment booking engine"
      }
    ]
  }
]
```

### 1.3 Check Subdomain Availability
- **Endpoint**: `GET /api/platform/onboarding/check-slug/?slug=<slug>`
- **Authentication**: None (Public)
- **Response `200 OK`**:
```json
{
  "slug": "city-hospital",
  "available": true,
  "reason": null
}
```
If unavailable or reserved:
```json
{
  "slug": "admin",
  "available": false,
  "reason": "This subdomain is reserved by the platform."
}
```

---

## 2. Authentication

### 2.1 Unified Login (Password or OTP)
- **Endpoint**: `POST /api/auth/login/`
- **Authentication**: None (Public)
- **Headers**:
  - Omit `X-Tenant` to log into the **Public Schema** (Platform Admins / Root Staff).
  - Include `X-Tenant: <slug>` to log into a **Tenant Workspace**.
- **Request Body (Password)**:
```json
{
  "username": "admin@ohm.com",
  "method": "password",
  "password": "SecretPassword123!"
}
```
- **Request Body (OTP)**:
```json
{
  "username": "+919999999999",
  "method": "otp",
  "otp": "123456"
}
```
- **Response `200 OK`**:
```json
{
  "message": "Login successful!",
  "schema_name": "public",
  "user": {
    "id": 1,
    "email": "admin@ohm.com",
    "full_name": "OHM Super Admin",
    "phone": "+919999999999",
    "is_active": true,
    "role": "Super Admin",
    "date_joined": "2026-10-21T10:00:00Z"
  },
  "tokens": {
    "refresh": "<jwt_refresh_token>",
    "access": "<jwt_access_token>"
  }
}
```

### 2.2 Request Login or Password-Reset OTP
- **Endpoint**: `POST /api/auth/otp/`
- **Authentication**: None (Public)
- **Headers**: Optional `X-Tenant`
- **Request Body**:
```json
{
  "username": "doctor@cityhospital.com",
  "purpose": "login"
}
```
*(Purpose can be `"login"` or `"password_reset"`)*
- **Response `202 Accepted`**:
```json
{
  "detail": "If the account exists, a verification code has been sent."
}
```

### 2.3 Reset Password via OTP
- **Endpoint**: `POST /api/auth/password-reset/`
- **Authentication**: None (Public)
- **Headers**: Optional `X-Tenant`
- **Request Body**:
```json
{
  "username": "doctor@cityhospital.com",
  "otp": "482910",
  "new_password": "NewStrongPassword456!"
}
```
- **Response `200 OK`**:
```json
{
  "detail": "Password has been reset."
}
```

### 2.4 Refresh Access Token
- **Endpoint**: `POST /api/auth/access-tokens/`
- **Authentication**: None
- **Request Body**:
```json
{
  "refresh": "<jwt_refresh_token>"
}
```
- **Response `200 OK`**:
```json
{
  "access": "<new_jwt_access_token>"
}
```

### 2.5 Unified Logout
- **Endpoint**: `POST /api/auth/logout/`
- **Authentication**: `Bearer <token>`
- **Request Body**:
```json
{
  "refresh_token": "<jwt_refresh_token>"
}
```
- **Response `200 OK`**:
```json
{
  "message": "Logout successful"
}
```

---

## 3. Tenant Customer Registration

### 3.1 Self-Register Customer Account
- **Endpoint**: `POST /api/auth/registrations/`
- **Authentication**: None
- **Headers**: `X-Tenant: <tenant-slug>` *(Required)*
- **Request Body**:
```json
{
  "email": "patient@example.com",
  "password": "PatientPassword789!",
  "full_name": "Jane Doe",
  "phone": "+918888888888"
}
```
- **Response `201 Created`**:
```json
{
  "email": "patient@example.com"
}
```

---

## 4. Tenant Workspace User Management

*All requests in this section require `Authorization: Bearer <token>`, `X-Tenant: <slug>`, and `IsTenantAdmin` permission.*

### 4.1 List Workspace Users
- **Endpoint**: `GET /api/users/`
- **Query Parameters**:
  - `role`: Filter by role code (e.g., `?role=TENANT_ADMIN` or `?role=CUSTOMER`)
  - `q`: Search email or full name (e.g., `?q=jane`)
- **Response `200 OK`**:
```json
[
  {
    "id": 1,
    "email": "owner@cityhospital.com",
    "full_name": "Dr. Sarah Miller",
    "phone": "+919999999999",
    "is_active": true,
    "role": "Tenant Admin",
    "date_joined": "2026-10-21T12:00:00Z"
  }
]
```

### 4.2 Create Workspace User
- **Endpoint**: `POST /api/users/`
- **Request Body**:
```json
{
  "email": "reception@cityhospital.com",
  "password": "StaffPassword123!",
  "full_name": "Mark Reception",
  "phone": "+917777777777",
  "role": "CUSTOMER"
}
```
- **Response `201 Created`**: Returns created user object.

### 4.3 Get Workspace User Details
- **Endpoint**: `GET /api/users/{id}/`
- **Response `200 OK`**: Returns user details.

### 4.4 Update Workspace User
- **Endpoint**: `PATCH /api/users/{id}/`
- **Request Body**:
```json
{
  "full_name": "Mark Jenkins",
  "is_active": true,
  "role": "CUSTOMER"
}
```
- **Response `200 OK`**: Returns updated user object.
> [!NOTE]
> Email cannot be changed. Deactivating the last active Tenant Admin is blocked with HTTP 400.

---

## 5. Provider Onboarding & Review

### 5.1 Submit Onboarding Application (Public)
- **Endpoint**: `POST /api/platform/onboarding/requests/`
- **Authentication**: None (Public)
- **Request Body**:
```json
{
  "contact_name": "Dr. John Doe",
  "contact_email": "johndoe@cityhospital.com",
  "contact_phone": "+919876543210",
  "organization_name": "City Hospital",
  "organization_type": "hospital",
  "organization_address": "123 Healthcare Ave, Metro City",
  "slug": "city-hospital",
  "custom_domain": "portal.cityhospital.com",
  "requested_plan": 1,
  "requested_modules": ["booking", "payroll", "notification"],
  "co_owner_emails": ["partner@cityhospital.com"],
  "password": "InitialAdminPassword123!"
}
```
- **Response `201 Created`**:
```json
{
  "id": "e2f18376-7ca1-4f76-802c-473d57861937",
  "status": "submitted"
}
```

### 5.2 List Onboarding Requests (Platform Admin)
- **Endpoint**: `GET /api/platform/onboarding/requests/`
- **Authentication**: `Bearer <token>` (Platform Admin)
- **Query Parameters**: `?status=submitted`
- **Response `200 OK`**: Array of onboarding request objects.

### 5.3 Retrieve Request Details
- **Endpoint**: `GET /api/platform/onboarding/requests/{id}/`
- **Authentication**: Platform Admin OR Header `X-Onboarding-Token: <access_token>`
- **Response `200 OK`**: Request details including review history.

### 5.4 Edit Submitted Application
- **Endpoint**: `PUT/PATCH /api/platform/onboarding/requests/{id}/`
- **Authentication**: Platform Admin OR Header `X-Onboarding-Token: <access_token>`

### 5.5 Approve Request & Trigger Provisioning
- **Endpoint**: `POST /api/platform/onboarding/requests/{id}/approve/`
- **Authentication**: Platform Admin
- **Request Body**:
```json
{
  "review_notes": "All compliance documents verified. Approved for launch."
}
```
- **Response `202 Accepted`**: Enqueues asynchronous Celery task `provision_tenant_task`.

### 5.6 Reject Request
- **Endpoint**: `POST /api/platform/onboarding/requests/{id}/reject/`
- **Authentication**: Platform Admin
- **Request Body**:
```json
{
  "review_notes": "Invalid registration license. Please re-submit with valid paperwork."
}
```
- **Response `200 OK`**: Status changed to `rejected`.

### 5.7 Retry Failed Provisioning
- **Endpoint**: `POST /api/platform/onboarding/requests/{id}/retry/`
- **Authentication**: Platform Admin
- **Response `202 Accepted`**: Re-enqueues provisioning for approved requests that failed previously.

---

## 6. Platform Tenant Administration

*Platform Admin only (`public` schema).*

### 6.1 List Tenants
- **Endpoint**: `GET /api/platform/tenants/`
- **Query Parameters**: `?status=active`, `?search=city`
- **Response `200 OK`**: Array of tenant profiles.

### 6.2 Create Tenant Directly
- **Endpoint**: `POST /api/platform/tenants/`
- **Request Body**:
```json
{
  "name": "Apex Clinic",
  "schema_name": "apex_clinic",
  "contact_email": "admin@apexclinic.com",
  "contact_phone": "+919999900000",
  "modules": ["booking", "wallet"],
  "owners": [1]
}
```
- **Response `201 Created`**: Automatically seeds default permissions and groups.

### 6.3 Suspend / Re-activate Tenant
- **Endpoint**: `POST /api/platform/tenants/{id}/suspend/`
- **Endpoint**: `POST /api/platform/tenants/{id}/activate/`
- **Response `200 OK`**: Updated tenant profile.

### 6.4 Delete Tenant Schema
- **Endpoint**: `DELETE /api/platform/tenants/{id}/?confirm=<schema_name>`
- **Prerequisite**: Tenant must be in `suspended` status.
- **Response `204 No Content`**: Completely drops the PostgreSQL schema and all tenant data.

### 6.5 Read / Update Tenant App Module Permissions
- **Endpoint**: `GET /api/platform/tenants/{id}/permissions/`
- **Endpoint**: `PATCH /api/platform/tenants/{id}/permissions/`
- **Request Body**:
```json
{
  "enabled_modules": ["booking", "analysis", "notification"]
}
```
- **Response `200 OK`**:
```json
{
  "tenant_id": 2,
  "enabled_modules": ["booking", "analysis", "notification"],
  "app_permissions": [
    {
      "code": "booking",
      "name": "Appointments",
      "description": "Patient booking management",
      "enabled": true
    }
  ]
}
```

### 6.6 Manage Object-Scoped Permissions Across Tenants
- **Endpoint**: `GET /api/platform/tenants/{id}/object-permissions/`
- **Endpoint**: `POST /api/platform/tenants/{id}/object-permissions/`
- **Request Body (Grant)**:
```json
{
  "user_id": 5,
  "role_code": "VENUE_ADMIN",
  "object_type": "venue_manager.venue",
  "object_id": 2
}
```
- **Endpoint**: `DELETE /api/platform/tenants/{id}/object-permissions/`
- **Request Body (Revoke)**:
```json
{
  "assignment_id": 14
}
```

---

## 7. Tenant Owner Workspace Portal

### 7.1 List Owned Tenants
- **Endpoint**: `GET /api/platform/my/tenants/`
- **Authentication**: `Bearer <token>` (Tenant Owner)
- **Response `200 OK`**: List of tenants where the authenticated user is registered as owner.

### 7.2 Update Workspace Profile & Branding
- **Endpoint**: `PATCH /api/platform/my/tenants/{id}/`
- **Content-Type**: `application/json` or `multipart/form-data`
- **Payload Fields**:
  - `name`: Organization name
  - `contact_email`, `contact_phone`
  - `timezone` (e.g., `"Asia/Kolkata"`)
  - `default_currency` (e.g., `"INR"`)
  - `logo`: Image file upload (multipart)
- **Response `200 OK`**: Updated profile.

### 7.3 Activate Provisioned Workspace
- **Endpoint**: `POST /api/platform/my/tenants/{id}/activate/`
- **Response `200 OK`**: Transitions status from `provisioning` to `active`.

---

## 8. Plans & Subscriptions

### 8.1 Plan Management
- `GET /api/platform/plans/` — List active plans (public) or all plans (admin).
- `POST /api/platform/plans/` — Create plan (admin).
- `GET/PUT/PATCH /api/platform/plans/{id}/` — Inspect or update plan.
- `DELETE /api/platform/plans/{id}/` — Delete plan (in-use plans cannot be deleted; soft-deactivate instead).

### 8.2 Subscription Management
- `GET /api/platform/subscriptions/` — List subscriptions (supports `?tenant=<id>&status=active`).
- `POST /api/platform/subscriptions/` — Create subscription:
```json
{
  "tenant": 2,
  "plan": 1,
  "status": "active",
  "start_date": "2026-10-21T00:00:00Z"
}
```
- `GET/PUT/PATCH/DELETE /api/platform/subscriptions/{id}/` — Manage subscriptions.

---

## 9. Owner & Co-owner Management

*Requires `Authorization: Bearer <token>` and `X-Tenant: <slug>` header.*

### 9.1 List Owners
- **Endpoint**: `GET /api/owner/`
- **Response `200 OK`**: Returns list of all co-owners with `TENANT_ADMIN` role.

### 9.2 Add Co-owner
- **Endpoint**: `POST /api/owner/`
- **Request Body**:
```json
{
  "email": "newowner@cityhospital.com",
  "full_name": "Dr. Alan Grant",
  "phone": "+919876500000"
}
```
- **Response `201 Created`**: Creates user and assigns `TENANT_ADMIN` role. Owners authenticate using OTP.

### 9.3 Inspect / Update Current Owner Profile (Self)
- **Endpoint**: `GET /api/owner/me/`
- **Endpoint**: `PATCH /api/owner/me/`
- **Request Body**:
```json
{
  "full_name": "Dr. Sarah Miller, MD",
  "phone": "+919999911111"
}
```

### 9.4 Remove / Deactivate Co-owner
- **Endpoint**: `DELETE /api/owner/{id}/`
- **Response `204 No Content`**: Soft-deactivates the user (`is_active = False`). Blocked if caller attempts to delete themselves or remove the last active owner.

---

## 10. Tenant Roles & Role Assignments

*Requires `IsTenantAdmin` and `X-Tenant: <slug>` header.*

### 10.1 List Roles
- **Endpoint**: `GET /api/roles/`
- **Response `200 OK`**:
```json
[
  {
    "id": 1,
    "code": "TENANT_ADMIN",
    "name": "Tenant Admin",
    "rank": 100,
    "permission_details": [
      {
        "id": 10,
        "code": "accounts.view_user",
        "name": "Can view user",
        "description": "Can view user for accounts.user"
      }
    ]
  }
]
```

### 10.2 Create Custom Role
- **Endpoint**: `POST /api/roles/`
- **Request Body**:
```json
{
  "code": "HEAD_NURSE",
  "name": "Head Nurse",
  "rank": 60,
  "permissions": [10, 11, 14]
}
```
- **Response `201 Created`**: Returns created role.

### 10.3 Delete Custom Role
- **Endpoint**: `DELETE /api/roles/{id}/`
- **Response `204 No Content`**: Deletes custom role. Built-in roles (`TENANT_ADMIN`, `CUSTOMER`) or roles assigned to active users cannot be deleted (returns HTTP 409).

### 10.4 List Available Permissions
- **Endpoint**: `GET /api/roles/permissions/`
- **Response `200 OK`**: Array of all available permission codes for role configuration.

### 10.5 List Role Assignments
- **Endpoint**: `GET /api/role-assignments/`
- **Query Parameters**: `?user_id=5`, `?role_id=2`
- **Response `200 OK`**:
```json
[
  {
    "id": 7,
    "user_id": 5,
    "user_email": "nurse@cityhospital.com",
    "role_id": 3,
    "role_code": "HEAD_NURSE",
    "role_name": "Head Nurse",
    "scope": null,
    "created_at": "2026-10-21T14:30:00Z"
  }
]
```

### 10.6 Assign Role Tenant-Wide or Object-Scoped
- **Endpoint**: `POST /api/role-assignments/`
- **Request Body (Tenant-Wide)**:
```json
{
  "user_id": 5,
  "role_id": 3
}
```
- **Request Body (Object-Scoped)**:
```json
{
  "user_id": 5,
  "role_id": 3,
  "object_type": "venue_manager.venue",
  "object_id": 2
}
```
- **Response `201 Created`**: Role assignment record.

### 10.7 Revoke Role Assignment
- **Endpoint**: `DELETE /api/role-assignments/{id}/`
- **Response `204 No Content`**: Revokes the assignment. Safeguard prevents deleting the last active workspace Tenant Admin assignment.
