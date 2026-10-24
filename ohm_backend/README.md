# OHM Backend

Multi-tenant healthcare aggregator platform built with **Django**, **django-tenants** (PostgreSQL schema-per-tenant architecture), **Django REST Framework (DRF)**, **SimpleJWT**, **Celery**, and **Redis**.

OHM manages healthcare service provider workspaces (hospitals, clinics, diagnostic centers) while ensuring strict tenant data isolation, custom domain support with automated SSL verification, fine-grained Role-Based Access Control (RBAC), and unified multi-schema authentication.

- For deep-dive architectural design, data isolation boundaries, and RBAC implementation, see [ARCHITECTURE.md](ARCHITECTURE.md).
- For complete endpoint specifications, request/response payloads, and status codes, see [API_DOCUMENTATION.md](API_DOCUMENTATION.md).
- Ready-to-import Postman collections and environments are provided in [`OHM Backend API.zip`](OHM%20Backend%20API.zip).

---

## Table of Contents
1. [First Run & Quickstart](#first-run--quickstart)
2. [Domain & Request Routing](#domain--request-routing)
3. [User Personas & Access Control](#user-personas--access-control)
4. [Automated Permissions & Role System](#automated-permissions--role-system)
5. [Management Commands](#management-commands)
6. [API Endpoints Overview](#api-endpoints-overview)
7. [Custom Domains & SSL Verification](#custom-domains--ssl-verification)
8. [Configuration & Environment Variables](#configuration--environment-variables)
9. [Adding Product Modules](#adding-product-modules)

---

## First Run & Quickstart

### Prerequisites
- Python 3.12+
- PostgreSQL 15+
- Redis 7+
- Docker & Docker Compose (optional, recommended for local services)

### 1. Configure Environment
```bash
cp .env.example .env
```
Ensure database credentials, `OHM_BASE_DOMAIN`, and `REDIS_URL` are configured properly in `.env`.

### 2. Launch Local Database & Broker (Docker)
```bash
docker compose up -d db redis
```

### 3. Apply Migrations
Generate migrations if changes are detected, then apply shared (public schema) migrations followed by tenant migrations:
```bash
# In local venv:
python manage.py makemigrations platform accounts
python manage.py migrate_schemas --shared
python manage.py migrate_schemas

# Or via Docker:
docker compose run --rm web python manage.py migrate_schemas --shared
docker compose run --rm web python manage.py migrate_schemas
```

### 4. Bootstrap Platform Super Admin & Root Hosts
Initialize the platform control plane in the `public` schema, register default hosts (`o-hm.com`, `admin.o-hm.com`, `api.o-hm.com`, and `OHM_API_HOST`), create the platform superuser, and assign all system permissions:
```bash
python manage.py bootstrap_platform \
  --email admin@ohm.com \
  --password admin \
  --full-name "OHM Super Admin" \
  --phone "+919999999999" \
  --title "Platform Administrator"
```

### 5. Seed Default Groups, Roles & Permissions
Automatically seed built-in groups and roles with their configured permission rules across both the public schema and existing tenant schemas:
```bash
python manage.py create_default_groups
```

### 6. Start Development Servers
Start the Django development server, Celery worker, and Celery beat:
```bash
# Terminal 1: API Server
python manage.py runserver 0.0.0.0:8000

# Terminal 2: Celery Worker
celery -A config worker -l info

# Terminal 3: Celery Beat Scheduler
celery -A config beat -l info
```
Verify the installation by hitting the health check:
```bash
curl http://localhost:8000/health/
# {"status": "ok", "schema": "public"}
```

---

## Domain & Request Routing

OHM exposes a unified API host (e.g. `api.ohm.com` in production or `api.ohm.localhost` in local development). Both platform administration and tenant workspace requests flow through this host:

```
                      React / Client Request
                                │
                                ▼
                       http://api.ohm.com
                                │
              ┌─────────────────┴─────────────────┐
       No X-Tenant Header                  X-Tenant: <slug>
              │                                   │
              ▼                                   ▼
        Public Schema                       Tenant Schema
    (Platform Admin / Ops)              (Provider Workspace)
```

- **Root / Public Requests**: When the `X-Tenant` header is omitted, `TenantHeaderMiddleware` retains the `public` schema. Platform endpoints (`/api/platform/*`), public catalogue (`/api/platform/plans/`), onboarding application submission, and unified platform admin login execute here.
- **Tenant Requests**: Include the header `X-Tenant: <tenant-slug>` (e.g., `X-Tenant: city-hospital`). The middleware resolves the slug against active tenants with verified domains, switches PostgreSQL's search path to that tenant's isolated schema before URL resolution, and ensures tenant-bound JWT tokens match the active schema.
- **Direct Custom Domains**: When a tenant accesses their verified custom domain directly (e.g., `portal.cityhospital.com`), `VerifiedDomainTenantMiddleware` automatically maps the host to the respective tenant schema.

---

## User Personas & Access Control

| Persona | Schema | Auth Method | Capabilities |
| :--- | :--- | :--- | :--- |
| **Super Admin** | `public` | Email/Phone + Password or OTP (`/api/auth/login/` without `X-Tenant`) | Full platform authority, reviews onboarding requests, manages tenant lifecycles, provisions workspaces, manages subscription plans and features. |
| **Platform Staff / Root User** | `public` | Email/Phone + Password or OTP | Operational staff managing onboarding applications, reviewing submissions, and viewing platform tenants. Has default permissions across `tenants`, `platform`, and `accounts`. |
| **Tenant Admin (Owner/Co-owner)** | Tenant schema (`public` mirror) | Email/Phone + Password or OTP (`/api/auth/login/` with `X-Tenant`) | Workspace administration: manages workspace users, invites co-owners, configures custom roles, manages role assignments, completes organization profile, and activates workspace. |
| **Operational Staff / Sub-roles** | Tenant schema | Email/Phone + Password or OTP (`/api/auth/login/` with `X-Tenant`) | Healthcare workspace staff assigned to specific operational roles (`OPS_MANAGER`, `VENUE_ADMIN`, `SERVICE_ADMIN`, `RESOURCE_ADMIN`, `STAFF`) with granular or object-scoped access. |
| **Customer / User** | Tenant schema | Email/Phone + Password or OTP (`/api/auth/login/` with `X-Tenant`) | Healthcare consumer / patient. Self-registers via `POST /api/auth/registrations/`. Has permissions on booking, wallet, and profile. |

---

## Automated Permissions & Role System

OHM uses a dual-layer permission architecture:
1. **Django `auth.Permission` & `auth.Group`**: Django standard authorization primitives utilized by permissions and admin.
2. **Product RBAC (`accounts.Role`, `accounts.Permission`, `accounts.RoleAssignment`)**: Tenant-configurable roles with ranks and object-level scoping.

### Predefined Roles & Permission Matrix
Roles automatically receive mapped permissions across modules via `GROUP_RULES`:

| Role Code | Display Name | Rank | Assigned Application Scopes & Actions |
| :--- | :--- | :---: | :--- |
| `SUPER_ADMIN` | Super Admin | 100 | **All system permissions** across public schema. |
| `TENANT_ADMIN` | Tenant Admin | 100 | **All actions** across tenant modules: `accounts`, `venue_manager`, `booking`, `payroll`, `notification`, `analysis`, `wallet`, `faq`. |
| `OPS_MANAGER` | Ops Manager | 80 | `view`, `change`, `add` across all operational tenant modules. |
| `VENUE_ADMIN` | Venue Admin | 70 | `view`, `change`, `add` across `venue_manager`, `booking`, `payroll`, `notification`. |
| `SERVICE_ADMIN` | Service Admin | 70 | `view`, `change`, `add` across `venue_manager`, `booking`, `payroll`, `notification`. |
| `RESOURCE_ADMIN` | Resource Admin | 70 | `view`, `change`, `add` across `venue_manager`, `booking`, `payroll`, `notification`. |
| `STAFF` | Staff Member | 50 | `view` across `venue_manager`, `booking`, `payroll`, `notification`. |
| `CUSTOMER` | Customer / User | 10 | `view`, `add` on `booking`, `wallet`, `accounts`. |

### Automated Lifecycle Signals
- **`post_schema_sync`**: Sets up each tenant schema's roles and permission groups using the same canonical definitions as `bootstrap_platform`.
- **`tenant_provisioned`**: Assigns the Tenant Admin role/group and creates mirrored user profiles for the applicant and all co-owners.
- **`post_save` on `User` & `PlatformAdmin`**: Automatically ensures public superusers receive all system permissions and public root staff receive default operational permissions.

---

## Management Commands

### 1. `bootstrap_platform`
Initializes or updates the platform control plane in the public schema. Idempotent.
```bash
python manage.py bootstrap_platform \
  --email <admin-email> \
  --password <admin-password> \
  [--full-name "Full Name"] \
  [--phone "+91..."] \
  [--title "Super Admin"] \
  [--reset-password]
```

### 2. `create_default_groups` / `assign_tenant_roles`
Synchronizes `auth.Permission` to `accounts.Permission`, creates default Django `Group`s and `Role`s, assigns rule-based permissions, and automatically mirrors all tenant owners (and co-owners) into their respective tenant schemas with `TENANT_ADMIN` role assignments, groups, and permissions. It also ensures platform superusers and staff users have proper permissions.
```bash
# Seed public schema and all tenant schemas (including existing owners and users):
python manage.py create_default_groups
# Or using the alias:
python manage.py assign_tenant_roles

# Configure a specific tenant schema:
python manage.py create_default_groups --schema=city_hospital
python manage.py assign_tenant_roles --schema=city_hospital

# Only seed groups/roles without altering user/owner assignments:
python manage.py create_default_groups --no-users
```

### 3. `provision_tenant`
Provisions an approved onboarding request into an active tenant schema from the CLI:
```bash
python manage.py provision_tenant <request_uuid>
```

### 4. `migrate_schemas`
Standard django-tenants migration command:
```bash
# Migrate shared (public) apps only:
python manage.py migrate_schemas --shared

# Migrate all tenant schemas:
python manage.py migrate_schemas --tenant

# Migrate a specific tenant:
python manage.py migrate_schemas -s city_hospital
```

---

## API Endpoints Overview

All endpoints are hosted on the unified API host (`api.ohm.com`).

### 1. Public Catalogue & Health
- `GET /health/` — Health check returning server status and current schema.
- `GET /api/platform/plans/` — List active subscription plans (public) or all plans (admin).
- `GET /api/platform/onboarding/check-slug/?slug=<slug>` — Live subdomain availability check.

### 2. Unified Authentication
- `POST /api/auth/login/` — Unified login using email or phone with password or OTP. Omit `X-Tenant` for platform staff; provide `X-Tenant: <slug>` for tenant users. Returns `tokens`, `user`, and `schema_name`.
- `POST /api/auth/otp/` — Request a 6-digit OTP for login or password reset (`username`, `purpose`).
- `POST /api/auth/password-reset/` — Reset password using OTP (`username`, `otp`, `new_password`).
- `POST /api/auth/access-tokens/` — Exchange refresh token for a new access token.
- `POST /api/auth/logout/` — Blacklist refresh token and invalidate session.

### 3. Tenant Customer Registration
- `POST /api/auth/registrations/` — Self-registration for healthcare consumers (requires `X-Tenant`). Automatically assigns `CUSTOMER` role.

### 4. Tenant Workspace User Management
*Requires `IsTenantAdmin` and `X-Tenant` header.*
- `GET /api/users/` — List users in the workspace (supports `?role=<role_code>` and `?q=<search>`).
- `POST /api/users/` — Create a workspace user with initial role and password.
- `GET /api/users/{id}/` — Retrieve user details.
- `PATCH /api/users/{id}/` — Update profile, role, or active status (safeguarded: cannot deactivate last Tenant Admin).

### 5. Provider Onboarding & Review
- `POST /api/platform/onboarding/requests/` — Public provider onboarding application submission. Returns `id` and one-time access token.
- `GET /api/platform/onboarding/requests/` — Platform Admin list of submitted applications (`?status=submitted`).
- `GET /api/platform/onboarding/requests/{id}/` — Inspect request details (Platform Admin or applicant with `X-Onboarding-Token`).
- `PUT/PATCH /api/platform/onboarding/requests/{id}/` — Edit submitted application.
- `POST /api/platform/onboarding/requests/{id}/approve/` — Approve application and enqueue asynchronous Celery provisioning.
- `POST /api/platform/onboarding/requests/{id}/reject/` — Reject onboarding request with review notes.
- `POST /api/platform/onboarding/requests/{id}/retry/` — Retry provisioning after an error.
- `DELETE /api/platform/onboarding/requests/{id}/` — Delete submitted or rejected request.

### 6. Platform Tenant Administration
*Platform Admin only (public schema).*
- `GET /api/platform/tenants/` — List managed tenants (excludes public tenant).
- `POST /api/platform/tenants/` — Create tenant directly with owner assignment.
- `GET /api/platform/tenants/{id}/` — Retrieve tenant metadata and domain list.
- `PUT/PATCH /api/platform/tenants/{id}/` — Update tenant name, status, or modules.
- `POST /api/platform/tenants/{id}/suspend/` — Suspend active tenant.
- `POST /api/platform/tenants/{id}/activate/` — Re-activate suspended tenant.
- `DELETE /api/platform/tenants/{id}/?confirm=<schema_name>` — Permanently drop tenant schema and data.
- `GET/PATCH /api/platform/tenants/{id}/permissions/` — Inspect or update tenant module feature flags (`enabled_modules`).
- `GET/POST/DELETE /api/platform/tenants/{id}/object-permissions/` — Inspect, grant, or revoke object-level role assignments within that tenant schema.

### 7. Tenant Owner Workspace Portal
*Requires authenticated tenant owner (`IsAuthenticated`).*
- `GET /api/platform/my/tenants/` — List tenants owned by caller.
- `GET /api/platform/my/tenants/{id}/` — Retrieve owned tenant details.
- `PATCH /api/platform/my/tenants/{id}/` — Update organization profile (supports JSON & multipart logo upload).
- `POST /api/platform/my/tenants/{id}/activate/` — Transition provisioned workspace to active state.

### 8. Plans & Subscriptions
*Public viewing for active plans; Platform Admin CRUD.*
- `GET/POST /api/platform/plans/` — List or create plans.
- `GET/PUT/PATCH/DELETE /api/platform/plans/{id}/` — Plan details, modification, or soft-deactivation.
- `GET/POST /api/platform/subscriptions/` — List or create tenant subscriptions.
- `GET/PUT/PATCH/DELETE /api/platform/subscriptions/{id}/` — Manage subscriptions.

### 9. Tenant Owner Management
*Requires `X-Tenant` header.*
- `GET /api/owner/` — List workspace co-owners.
- `POST /api/owner/` — Add a co-owner (seeded with `TENANT_ADMIN` role).
- `GET /api/owner/me/` — Retrieve authenticated user's owner profile.
- `PATCH /api/owner/me/` — Update authenticated user's owner profile. Changes to name, phone, active status, or password are synchronized with the public-schema owner account and the user's other tenant-owner mirrors.
- `GET/PUT/PATCH /api/owner/{id}/` — Inspect or update owner account.
- `DELETE /api/owner/{id}/` — Soft-deactivate co-owner (protected: cannot remove self or last active owner).

### 10. Tenant Roles & Role Assignments
*Requires `IsTenantAdmin` and `X-Tenant` header.*
- `GET /api/roles/` — List workspace roles with permission breakdown.
- `POST /api/roles/` — Create custom workspace role (`code`, `name`, `rank`, `permissions`).
- `GET/PUT/PATCH /api/roles/{id}/` — Retrieve or modify custom role.
- `DELETE /api/roles/{id}/` — Delete custom role (protected: built-in roles and assigned roles cannot be deleted).
- `GET /api/roles/permissions/` — List all assignable permission codes.
- `GET /api/role-assignments/` — List role assignments (`?user_id=`, `?role_id=`).
- `POST /api/role-assignments/` — Assign role tenant-wide or scoped to an object (`object_type`, `object_id`).
- `DELETE /api/role-assignments/{id}/` — Revoke role assignment.

*See [API_DOCUMENTATION.md](API_DOCUMENTATION.md) for complete payload and response schemas.*

---

## Custom Domains & SSL Verification

1. **Tenant Configuration**: The tenant registers DNS records:
   - `CNAME  portal.example.com  ->  <OHM_EDGE_HOST>`
   - `TXT    _ohm-verify.portal.example.com  =  <verification_token>`
2. **Automated Verification**: Celery beat task `apps.platform.tasks.verify_pending_domains` runs every 5 minutes:
   - Queries DNS for TXT and CNAME matches.
   - Upon verification, sets `Domain.verified = True` and timestamps `verified_at`.
3. **Caddy Reverse Proxy Integration**: Caddy checks `/internal/domain-check/` before issuing an SSL certificate. Unverified or unrecognized domains are rejected with HTTP 404.

---

## Configuration & Environment Variables

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `DJANGO_SECRET_KEY` | *required in prod* | Secret key for cryptographic signing. |
| `DJANGO_DEBUG` | `True` | Debug flag (set `False` in production). |
| `OHM_BASE_DOMAIN` | `localhost` | Platform base domain (e.g., `ohm.com`). |
| `OHM_API_HOST` | `api.<OHM_BASE_DOMAIN>` | Shared API hostname (e.g., `api.ohm.com`). |
| `OHM_ROOT_HOSTS` | `127.0.0.1,localhost` | Comma-separated allowed root platform hosts. |
| `OHM_EDGE_HOST` | `edge.example.com` | Edge ingress target for tenant CNAME records. |
| `OHM_INTERNAL_TOKEN`| `dev-internal-token`| Shared secret for internal health and Caddy checks. |
| `OHM_URL_SCHEME` | `http` | URL scheme (`https` in production). |
| `POSTGRES_DB` | `ohm` | PostgreSQL database name. |
| `POSTGRES_USER` | `ohm` | PostgreSQL database user. |
| `POSTGRES_PASSWORD` | `ohm` | PostgreSQL database password. |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host. |
| `POSTGRES_PORT` | `5432` | PostgreSQL port. |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL for Celery and caching. |
| `EMAIL_HOST` | `localhost` | SMTP host for email delivery. |
| `EMAIL_PORT` | `587` | SMTP port. |
| `EMAIL_HOST_USER` | `""` | SMTP username. |
| `EMAIL_HOST_PASSWORD`| `""` | SMTP password. |
| `DEFAULT_FROM_EMAIL`| `noreply@ohm.local` | Outgoing email sender address. |
| `OHM_SMS_OTP_BACKEND`| `""` | Dotted Python path to custom SMS OTP sender callable. |

---

## Adding Product Modules

To introduce a new operational feature (e.g. `booking`, `invoices`, `records`):

1. **Create the Django App**: Add model classes in `apps/<module_name>/models.py`.
2. **Register in `TENANT_APPS`**: Add `"apps.<module_name>"` to `TENANT_APPS` in `config/settings.py` (do not add to `SHARED_APPS` unless it requires control-plane storage).
3. **Define Permission Mappings**: Update `GROUP_RULES` in `apps/accounts/services.py` with allowed actions for each role.
4. **Mount Endpoints**: Include module URLs in `config/urls.py` or `apps/accounts/urls.py`.
5. **Run Migrations**:
   ```bash
   python manage.py makemigrations <module_name>
   python manage.py migrate_schemas --tenant
   python manage.py create_default_groups
   ```
