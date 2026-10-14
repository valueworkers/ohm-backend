# ohm-backend

Multi-tenant medicare platform. django-tenants (schema-per-tenant, PostgreSQL), one verified custom domain per tenant,
Django REST Framework + JWT, Celery + Redis.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the boundary between OHM's public platform schema and provider-owned tenant schemas.

## First run
    cp .env.example .env
    docker compose up -d db redis
    docker compose run --rm web python manage.py makemigrations tenants accounts   # if migrations not present
    docker compose run --rm web python manage.py migrate_schemas --shared
    docker compose run --rm web python manage.py bootstrap_platform --email you@example.com --password '...'
    docker compose up web worker beat

The API uses one host for platform and tenant requests. Locally configure `OHM_API_HOST=api.ohm.localhost`, run
`bootstrap_platform`, then use `http://api.ohm.localhost:8000/health/`. Tenant API requests also send
`X-Tenant: <tenant-slug>` (for example, `X-Tenant: vaishnavi-medicare`).

## Personas
    O-HM platform (public schema)      -> Super Admin   (Django admin at /admin/ on the root host)
    Tenant (own schema + domain)       -> Tenant Admin  -> Customer / User
- **Super Admin** reviews provider onboarding requests and provisions approved tenants in the public schema.
  Root users are created in Django admin or with `bootstrap_platform`.
- **Tenant Admin** (first user of a tenant) manages the workspace through `/api/users/` (list/create/update, deactivate
  not delete; a workspace always keeps one active Tenant Admin).
- **Customer / User** signs up at `POST /api/auth/registrations/` and logs in at `/api/auth/login/`.
- Venue/Service/Resource Admin, Ops Manager and Staff exist in `Role.Code` but are not seeded until those features ship.

## Shared API host
Platform endpoints are defined in `apps/platform/views.py`, `serializers.py`, and `urls.py`.
Tenant account endpoints are grouped in `apps/accounts/views.py` and `urls.py`: `/api/auth/` handles sign-in,
tokens, and the current user's profile; `/api/users/` lets Tenant Admins manage workspace accounts.
`OHM_API_HOST` (default `api.<OHM_BASE_DOMAIN>`) is registered as a public-schema host by `bootstrap_platform`.
Set `OHM_API_HOST=api.ohm.com` in production. React calls that API host from `ohm.com` and tenant subdomains.
Tenant requests include `X-Tenant: <slug>`; root requests omit the header and remain in the public schema.
Configure `OHM_CORS_ALLOWED_ORIGINS` and `OHM_CORS_ALLOWED_ORIGIN_REGEXES` for the React origins. On the API host:
- `/admin/`  platform admin (Super Admin / Our Team)
- `POST /api/auth/login/`  unified root or tenant login using email/mobile plus password or OTP; include `X-Tenant` for tenant accounts and omit it for platform admins
- `POST /api/auth/otp/`  request a login or password-reset OTP (`username`, `purpose`); send `X-Tenant` for tenant accounts
- `POST /api/auth/password-reset/`  reset with `username`, `otp`, and `new_password`; send `X-Tenant` for tenant accounts
- `POST /api/onboarding/requests/`  submit a public multi-step provider application
- `GET /api/onboarding/requests/`  root-only list of applications
- `GET /api/onboarding/requests/{uuid}/`  root inspection or applicant status check (applicants send `X-Onboarding-Token`)
- `PATCH /api/onboarding/requests/{uuid}/`  applicant edits or root status/review updates
- `POST /api/platform/tenants/`  create a tenant from an approved onboarding request
- `/api/platform/tenants/`  root-only tenant collection; the public platform tenant is excluded
- `GET /api/platform/tenants/` and `GET /api/platform/tenants/{id}/`  list or inspect tenants
- `PUT/PATCH /api/platform/tenants/{id}/`  update a tenant's name, status, or enabled modules
- `DELETE /api/platform/tenants/{id}/`  permanently delete the tenant and its schema
- `POST /api/platform/login/`  `{email, password}` -> JWTs for an active root superuser (omit `X-Tenant`)
- `POST /api/auth/registrations/`  create a customer account (include `X-Tenant`)
- `POST /api/auth/login/`  `{email, password}` -> tenant-bound JWTs (include `X-Tenant`)
- `POST /api/auth/access-tokens/`  exchange a refresh token for a new access token
- `GET /api/auth/session/`  return the current authenticated tenant user and workspace

The onboarding form submits `contact_name`, `contact_email`, `contact_phone`, `organization_name`,
`organization_type`, `organization_address`, `slug`, `custom_domain`, `requested_modules`, and the initial admin
`password`. The response includes a request UUID
and a one-time `access_token`; keep it and send it as `X-Onboarding-Token` when reading or updating that request.
The password is stored as a hash and cleared after provisioning or rejection. Requests do not create schemas or
domains until a root admin approves the request and creates the tenant from it.

For local frontend development, point the frontend origin at an allowed CORS origin. In production set `OHM_BASE_DOMAIN=ohm.com`,
`OHM_URL_SCHEME=https`, `OHM_URL_PORT=` and point `ohm.com` and `*.ohm.com` at the server.

OTP codes are stored in Redis, expire after five minutes, and are single-use. Email delivery uses Django's `EMAIL_*` settings. For mobile numbers,
configure `OHM_SMS_OTP_BACKEND` with a dotted-path callable accepting `phone`, `code`, and `purpose` keyword arguments. OTP
requests are rate limited; accounts must have a unique phone number in their schema to use phone-based login. Redis uses
the configured `REDIS_URL` with the `ohm` key prefix.

## Custom domains
1. Tenant adds `CNAME app.example.com -> $OHM_EDGE_HOST` and `TXT _ohm-verify.app.example.com = <token>`.
2. Celery beat (`verify_pending_domains`, every 5 min) flips `Domain.verified`.
3. Caddy asks `/internal/domain-check/` before issuing a cert; unverified hosts get no cert and 404.

## Adding a product module
Create an app, add it to `TENANT_APPS`, mount its URLs in `config/urls.py` with
`permission_classes=[ModuleEnabled("booking")]`, and enable it per tenant via `Tenant.modules`.

## Migrating tenants
`python manage.py migrate_schemas` (all schemas) or `--tenant` / `-s <schema>`.
Tenant-scoped Celery tasks use `base=TenantTask` and are sent with `kwargs={"schema_name": ...}`.
