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

> [!NOTE]
> **Super User Tenant Access**: Platform Super Users can access any tenant schema using their public credentials:
> 1. **Direct Workspace Login**: Call `POST /api/auth/login/` with `X-Tenant: <slug>` and Super User credentials. The system authenticates against the canonical public account, automatically mirrors the user into the tenant schema with full `Super User` and `Tenant Admin` privileges, and issues a tenant-scoped session and tokens.
> 2. **Cross-Schema Token Access**: Present a public-schema Super User JWT access token (`Authorization: Bearer <public_token>`) alongside `X-Tenant: <slug>` on any tenant API endpoint. The authentication backend verifies the active public session, provisions/synchronizes the superuser in the tenant schema, and grants full administrative access.

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

This reset endpoint supports both password setup for accounts without a password and
password recovery. It applies Django's configured password validators and revokes
active sessions. It works in the public schema and tenant schemas; provisioned tenant
owners use their activated account identity and their password stays in sync across
the public account and tenant mirrors.

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
- Refresh tokens rotate per session. Replaying an older refresh token or using it in a different schema is rejected. Access tokens expire after 15 minutes; existing tokens without a session identifier require a fresh login after deployment.

### 2.5 Unified Logout
- **Endpoint**: `POST /api/auth/logout/`
- **Authentication**: `Bearer <token>`
- **Request Body**:
```json
{ "refresh_token": "<current_session_refresh_token>" }
```
- The `refresh_token` is optional when the request uses a valid session-bound access token. If supplied, it must match that same active session. Logout revokes the server-side session, invalidating its access and refresh tokens.
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
  "co_owner_emails": ["partner@cityhospital.com"],
  "password": "InitialAdminPassword123!"
}
```
- `requested_plan` may be omitted; plan ID `1` (the default Trial plan) is used. Provisioning subscribes the tenant to the chosen plan and copies its active features to the tenant's enabled modules. The default Trial plan creates a trial subscription with a 14-day trial period.
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
  "requested_plan": 1,
  "review_notes": "All compliance documents verified. Approved for launch."
}
```
- `requested_plan` is optional; the platform admin may provide it to select or change the plan. Otherwise, the application's plan is used, falling back to plan ID `1` (the default Trial plan). Provisioning creates the subscription and enables that plan's active features. Plan ID `1` creates a trial subscription with a 14-day trial period.
- **Response `202 Accepted`**: Enqueues asynchronous Celery task `provision_tenant_task`; the subscription is created by that provisioning task.

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
  "plan": 1,
  "owners": [1]
}
```
- **Response `201 Created`**: Automatically seeds default permissions and groups.
- `plan` may be omitted; plan ID `1` (the default Trial plan) is used. If supplied, it must reference an active plan. The tenant is subscribed to the plan during creation; its active features set enabled modules. Plan ID `1` creates a trial subscription with a 14-day trial period.

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
- Plan responses include the current billing period, currency, amount, setup fee, and tax rate. Platform admins edit these fields on the plan in Django Admin.

### 8.2 Subscription Management
- `GET /api/platform/subscriptions/` — List subscriptions (supports `?tenant=<id>&status=active`).
- `POST /api/platform/subscriptions/` — Create subscription:
```json
{
  "tenant": 2,
  "plan": 1,
  "status": "active",
  "starts_at": "2026-10-21T00:00:00Z"
}
```
- `GET/PUT/PATCH/DELETE /api/platform/subscriptions/{id}/` — Manage subscriptions.
- Subscription responses include `price_details`, a snapshot of the plan pricing selected when the subscription was created or invoiced.

### 8.3 Billing records and workflow
- `Quotation`, `Invoice`, and `Payment` are managed in the platform Django Admin. Quotation admin actions mark quotes sent or accepted and create invoices; invoice actions issue draft invoices. Sending a quote changes its workflow status; email delivery is not wired into this action.
- Billing calculations, concurrent-safe `QT-YYYY-NNNN` / `INV-YYYY-NNNN` numbering, invoice payment-status updates, and subscription activation/extension are implemented in `apps.platform.services`.
- A provider-specific payment webhook endpoint is not configured. A gateway integration must verify the provider signature before calling `record_gateway_payment`; the service uses the gateway/payment ID as an idempotency key.

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

## 10. Permission Groups (Roles) & Django Permissions

*The RBAC system utilizes Django's native Permissions (`auth.Permission`) and Permission Groups (`auth.Group`) as roles, mirroring the Django Admin dashboard's permission structure. This eliminates the need to manage roles and role assignments as separate entities. Managed by Superadmin and Root user in the Public Schema, and Tenant Admin in tenant workspaces.*

### 10.1 List Permission Groups (Roles)
- **Endpoint**: `GET /api/groups/`
- **Query Parameters**: `?search=nurse`, `?ordering=name`
- **Response `200 OK`**:
```json
[
  {
    "id": 1,
    "name": "Head Nurse",
    "permissions": [12, 13, 14],
    "permission_details": [
      {
        "id": 12,
        "name": "Can view venue",
        "codename": "view_venue",
        "app_label": "catalog",
        "model": "venue"
      }
    ],
    "user_count": 5
  }
]
```

### 10.2 Create Permission Group (Role)
- **Endpoint**: `POST /api/groups/`
- **Request Body**:
```json
{
  "name": "Booking Coordinators",
  "permissions": [12, 13, 14]
}
```
- **Response `201 Created`**: Returns created group with assigned permissions.

### 10.3 Retrieve Permission Group
- **Endpoint**: `GET /api/groups/{id}/`
- **Response `200 OK`**: Returns group details, permissions array, permission details, and user count.

### 10.4 Update Permission Group
- **Endpoint**: `PUT /api/groups/{id}/` / `PATCH /api/groups/{id}/`
- **Request Body**:
```json
{
  "name": "Lead Booking Coordinators",
  "permissions": [12, 13, 14, 15]
}
```
- **Response `200 OK`**: Returns updated group.

### 10.5 Delete Permission Group
- **Endpoint**: `DELETE /api/groups/{id}/`
- **Response `204 No Content`**: Deletes group. Built-in groups (`Super User`, `Tenant Admin`) cannot be deleted.

### 10.6 Manage Group Members (Assign / Remove Role)
- **List members**: `GET /api/groups/{id}/users/`
- **Add members**: `POST /api/groups/{id}/users/`
```json
{
  "user_ids": [5, 6]
}
```
- **Remove member**: `DELETE /api/groups/{id}/users/` with body `{"user_id": 5}` or query parameter `?user_id=5`.

### 10.7 List Django Permissions
- **Endpoint**: `GET /api/permissions/`
- **Query Parameters**: `?search=booking`, `?content_type__app_label=catalog`
- **Response `200 OK`**: Array of installed Django permissions with `id`, `name`, `codename`, `app_label`, and `model`.

### 10.8 List Django Permissions Grouped (Django Admin Dashboard Structure)
- **Endpoint**: `GET /api/permissions/grouped/`
- **Response `200 OK`**:
```json
{
  "accounts": {
    "user": [
      {"id": 1, "name": "Can add user", "codename": "add_user"},
      {"id": 2, "name": "Can change user", "codename": "change_user"},
      {"id": 3, "name": "Can delete user", "codename": "delete_user"},
      {"id": 4, "name": "Can view user", "codename": "view_user"}
    ]
  },
  "catalog": {
    "venue": [
      {"id": 12, "name": "Can view venue", "codename": "view_venue"}
    ]
  }
}
```

### 10.9 User Permissions & Group Assignment (Django Admin User Model)
Tenants assign permission groups and direct model permissions directly on the User (`/api/users/`), mirroring Django Admin's user management interface:
- **`groups`**: Array of group IDs assigned to the user (acts as roles).
- **`user_permissions`**: Array of direct Django permission IDs granted specifically to this user.
- **`group_details`**: Summary array of assigned groups with `id` and `name`.
- **`user_permission_details`**: Summary array of direct permissions with `id`, `name`, `codename`, `app_label`, and `model`.

## 12. User Settings

The settings panel has three scopes. Platform Super Users use the personal settings routes below in the public schema and manage any workspace with `/api/platform/tenants/{id}/` (platform admin only). A tenant owner manages their organization profile and branding through `/api/platform/my/tenants/{id}/` (owner only; public schema). Each tenant user manages their own profile and personal preferences through the same `/api/settings/*` routes while authenticated in that tenant schema. Personal preference records are isolated per schema and per user; organization profile changes do not modify individual user preferences.

All personal settings routes require `Authorization: Bearer <token>` and operate on the authenticated user in the active schema. `GET/PATCH /api/settings/profile/` reads or updates trimmed `first_name` and `last_name` (30 characters maximum); email is read-only here. `POST /api/settings/email/change/` accepts `{ "email": "new@example.com" }` and emails a one-time token. `POST /api/settings/email/verify/` accepts `{ "token": "..." }`; only successful verification changes the address and marks it verified. Tokens expire after 30 minutes and cannot be reused. `POST /api/settings/email/resend-verification/` resends for the current pending address. These mail endpoints are limited to five requests per hour per authenticated user.

`GET/PATCH /api/settings/appearance/` stores `theme` (`light`, `dark`, `system`), `font_size` (`small`, `medium`, `large`), and `font_family` (`system`, `inter`, `roboto`, `open_sans`, `lato`). `GET/PATCH /api/settings/notifications/` accepts partial `channels` and `categories` maps of booleans. Supported channels are `email`, `in_app`, `browser_push`, `mobile_push`, and `sms`; supported categories are `product_updates`, `marketing_emails`, `security_alerts`, and `billing_alerts`. Security and billing alerts are always enabled. Marketing preference updates record or clear `marketing_consent_at`.

`POST /api/auth/password-reset/` sets or resets a password using a password-reset OTP. Password changes and resets revoke all active sessions, so users must sign in again. `PUT /api/profile/` does not accept password changes.

The existing `POST /api/change-password/` endpoint handles password changes with current-password verification and Django's configured password validators. `GET /api/settings/security/sessions/` returns the authenticated user's paginated session inventory. `POST /api/settings/security/sessions/revoke-all/` revokes all other sessions and requires the current password or a fresh login OTP. Billing-provider operations, 2FA, recovery codes, backups, and import/export jobs are not implemented by this backend and are not exposed as nonfunctional settings endpoints. Email verification delivery uses the configured Django email backend.

## Prospect Quotes

Prospect quotes are available to platform admins in the public schema and to authenticated tenant users with `X-Tenant: <slug>`. Tenant quote and analytics queries are scoped to the active tenant.

- `GET/PATCH /api/platform/quotes/tax-settings/` — Read or update the seller GST state for the current scope. Public settings are seeded as Karnataka (`29`); each tenant has its own record, initialized from its GSTIN when available.
- `GET/POST /api/platform/quotes/` — List quotes or create a draft.
- `GET/PATCH /api/platform/quotes/{id}/` — Retrieve or update a draft.
- `POST /api/platform/quotes/{id}/send/`, `/send-test/`, `/revise/` — Send, test-send, or create a revision.
- `GET /api/platform/quotes/{id}/pdf/` — Download a PDF.
- `GET/POST /api/platform/public/quotes/{accept_token}/respond/` — Publicly view and accept or reject a sent quote.
- `GET /api/platform/quotes/stats/` — Quote analytics for the current scope.

---

## 13. Care Product Module APIs (`/api/care/`)

The Care product module provides clinical, booking order, and patient lifecycle management for healthcare and care provider workspaces. All endpoints are routed under `/api/care/` (with `/api/care/` supported as a backward-compatible alias). All endpoints are scoped strictly to the calling tenant's isolated schema and require active plan entitlement.

### Prerequisites & Headers
- **`X-Tenant: <schema-slug>`** (Required): Routes the request to the tenant workspace.
- **`Authorization: Bearer <token>`** (Required): Authentication JWT containing matching `schema` claim.
- **Plan Entitlement**: Tenant must have an active subscription including feature code `"care"` or `"medicare"` (or `Tenant.modules` containing `"care"`). Unentitled requests receive `403 Forbidden` (`"Your subscription plan does not include access to the 'care' module."`).

---

### 13.1 Patients & Clinical Records

#### List / Create Patients
- **`GET /api/care/patients/`**
  - **Query Params**: `search` (name, phone, patient_id), `is_deleted` (default: false), `status` (active boolean), `page`, `page_size`.
  - **Response `200 OK`**: Paginated array of patient records with auto-generated identifiers (`patient_id`: `"PAT-00001"`).
- **`POST /api/care/patients/`**
  - **Payload**:
    ```json
    {
      "first_name": "Aarav",
      "last_name": "Sharma",
      "phone": "+919876543210",
      "email": "aarav.sharma@example.com",
      "dob": "1988-06-15",
      "gender": "MALE",
      "blood_group": "B+",
      "address": {
        "line1": "Flat 402, Lotus Residency",
        "line2": "MG Road",
        "locality": "Indiranagar",
        "city": "Bengaluru",
        "state": "Karnataka",
        "pincode": "560038"
      },
      "medical_history": "Hypertension, mild asthma",
      "allergies": "Penicillin",
      "emergency_contact_name": "Priya Sharma",
      "emergency_contact_phone": "+919876543211",
      "emergency_contact_relation": "Spouse"
    }
    ```
  - **Response `201 Created`**: Returns created patient object with sequential `patient_id`.

#### Retrieve / Update / Soft-Delete Patient
- **`GET /api/care/patients/{id}/`**
- **`PATCH /api/care/patients/{id}/`**
- **`DELETE /api/care/patients/{id}/`**
  - Soft-deletes the patient (`is_deleted=true`, `status=false`, `deleted_at` timestamp). Returns `204 No Content`.
- **`PATCH /api/care/patients/{id}/active-status/`**
  - **Payload**: `{"status": false}`
  - Explicitly toggle active status without deleting.

#### Patient Documents
- **`GET /api/care/patient-documents/?patient={patient_id}`**
- **`POST /api/care/patient-documents/`** (Multipart form data for document files).

---

### 13.2 Packages & Care Catalog

Packages define pricing structures for healthcare delivery (hourly, daily, weekly, monthly). Each package targets **exactly one** catalog entity (`venue`, `service`, or `resource`), enforced at both serializer and database constraint levels.

#### List / Create Packages
- **`GET /api/care/packages/`**
- **`POST /api/care/packages/`**
  - **Payload**:
    ```json
    {
      "name": "Intensive Post-Op Nursing (Daily)",
      "package_type": "IN_HOUSE",
      "period": "DAILY",
      "price": "2500.00",
      "registration_fees": "500.00",
      "service": 12,
      "description": "Daily 12-hour specialized nursing care."
    }
    ```
  - **Validation**: Supplying multiple targets (e.g. `service` and `venue`) or zero targets returns `400 Bad Request` (`"Package must be linked to exactly one target entity"`).

---

### 13.3 Restructured Booking Orders

OHM decouples overarching patient agreements (`PrimaryOrder`) from specific operational cycles (`SecondaryOrder`) and add-on care items (`TernaryOrder`).

```
PrimaryOrder (Patient Agreement & Cumulative Bill)
   ├── SecondaryOrder (Cycle 1: Package, Service, Venue, Dates, Base Amount)
   │     ├── TernaryOrder (Add-on Service / Rental Resource)
   │     └── TernaryOrder (Specialized Diagnostic Procedure)
   └── SecondaryOrder (Cycle 2: Auto-continued next period)
```

#### Primary Orders
- **`GET /api/care/primary-orders/`**
- **`POST /api/care/primary-orders/`**
  - **Payload**:
    ```json
    {
      "patient": 5,
      "notes": "Post knee arthroplasty home rehabilitation care plan"
    }
    ```
  - **Response `201 Created`**: Returns order with `order_id` format `#P00005000000`.

#### Secondary Orders
- **`GET /api/care/secondary-orders/`**
- **`POST /api/care/secondary-orders/`**
  - **Payload**:
    ```json
    {
      "primary_order": 1,
      "package": 3,
      "service": 12,
      "booking_type": "CLIENT_SIDE",
      "client_address": {
        "line1": "Flat 402, Lotus Residency",
        "city": "Bengaluru",
        "pincode": "560038"
      },
      "start_datetime": "2026-11-20T09:00:00Z",
      "end_datetime": "2026-11-27T09:00:00Z",
      "subtotal": "17500.00",
      "discount_amount": "1000.00",
      "auto_continue": true
    }
    ```
  - Automatically updates parent `PrimaryOrder.total_bill` and start/end date ranges.

#### Generate Invoice for Secondary Order
- **`POST /api/care/secondary-orders/{id}/generate-invoice/`**
  - Generates or retrieves an itemized `apps.billing.Invoice` (`SINV-YYYY-XXXXX`) calculating CGST, SGST, IGST using the provider's tax settings.
  - **Response `200 OK`**:
    ```json
    {
      "secondary_order_id": "#P00001S0001",
      "invoice_id": 42,
      "invoice_number": "SINV-2026-00001",
      "status": "DRAFT",
      "total_amount": "19470.00"
    }
    ```

#### Ternary Orders (Add-ons)
- **`GET /api/care/ternary-orders/`**
- **`POST /api/care/ternary-orders/`**
  - Links an add-on `service`, `resource`, or `package` to an existing `SecondaryOrder`. Recomputes parent secondary total amount.
- **`POST /api/care/ternary-orders/{id}/generate-invoice/`**
  - Generates an add-on itemized invoice (`TINV-YYYY-XXXXX`).

---

### 13.4 Payments & Per-Tenant Razorpay

#### Tenant Payment Credentials Configuration
- **`GET /api/care/payment-config/`**
- **`PATCH /api/care/payment-config/`**
  - **Payload**:
    ```json
    {
      "razorpay_key_id": "rzp_live_abc123",
      "razorpay_key_secret": "secret_key_xyz",
      "razorpay_webhook_secret": "webhook_secret_789",
      "currency": "INR",
      "is_active": true
    }
    ```

#### Razorpay Order Initiation
- **`POST /api/care/razorpay/initiate/`**
  - **Payload**: `{"invoice_id": 42}`
  - Dynamically initializes Razorpay client using this tenant's stored credentials, creates an order with Razorpay, and returns order metadata.

#### Razorpay Payment Signature Verification
- **`POST /api/care/razorpay/verify/`**
  - **Payload**:
    ```json
    {
      "invoice_id": 42,
      "razorpay_order_id": "order_EKfUsGQqqPt03x",
      "razorpay_payment_id": "pay_29QQoUBi66xm2f",
      "razorpay_signature": "9ef4b0de9566e7314f777cb4e7a80280080142a204c0983d2495088ecd517b96"
    }
    ```
  - Verifies HMAC SHA-256 signature, creates verified `billing.Payment` record in the tenant schema, adjusts `paid_amount` and `remaining_amount`, and sets invoice status to `PAID` or `PARTIALLY_PAID`.

---

### 13.5 Staff Attendance

- **`GET /api/care/attendance/`** — Paginated attendance logs. Filter by `date`, `user`, `status`.
- **`POST /api/care/attendance/`** — Log attendance check-in.
- **`POST /api/care/attendance/mark-today/`** — Batch-marks active staff present today (used also by Celery beat).

