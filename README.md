# OHM Backend

Multi-tenant healthcare aggregator platform built on **Django**, **django-tenants** (PostgreSQL schema-per-tenant), **Django REST Framework**, **SimpleJWT**, **Celery**, and **Redis**.

OHM enables hospitals, clinics, and healthcare service providers to run isolated digital workspaces under a unified platform control plane, complete with verified custom domains, dual-layer RBAC, and automated background provisioning.

---

## Documentation Quick Links

| Document | Purpose |
| :--- | :--- |
| **[Backend Guide (`ohm_backend/README.md`)](ohm_backend/README.md)** | Getting started, management commands, user personas, routing, and deployment. |
| **[Architecture Guide (`ohm_backend/ARCHITECTURE.md`)](ohm_backend/ARCHITECTURE.md)** | Multi-tenant schema design, HIPAA data isolation, RBAC matrices, and async workflows. |
| **[API Reference (`ohm_backend/API_DOCUMENTATION.md`)](ohm_backend/API_DOCUMENTATION.md)** | Complete HTTP API documentation covering all 11 modules with request/response schemas. |
| **[Postman Collections (`ohm_backend/OHM Backend API.zip`)](ohm_backend/OHM%20Backend%20API.zip)** | Pre-configured Postman collection and environment files (`Develop`, `Testing`, `Prod`). |

---

## Tech Stack

- **Framework**: Django 5+ / 6+ with Django REST Framework
- **Multi-Tenancy**: `django-tenants` (PostgreSQL schema-per-tenant isolation)
- **Database**: PostgreSQL 15+
- **Asynchronous Tasks & Scheduling**: Celery & Celery Beat
- **Message Broker & Cache**: Redis
- **Authentication**: SimpleJWT (schema-bound access & refresh tokens) + Redis OTP
- **Reverse Proxy & SSL**: Caddy with on-demand TLS verification

---

## Repository Structure

```
ohm-backend/
├── .github/
│   └── workflows/
│       └── deploy.yml              # CI/CD deployment pipeline to DigitalOcean
├── ohm_backend/                    # Django project root
│   ├── apps/
│   │   ├── accounts/               # Auth, users, RBAC roles, permissions & signals
│   │   ├── platform/               # Control plane, tenants, plans, subscriptions, onboarding
│   │   ├── booking/                # Operational module: appointment scheduling
│   │   ├── venue_manager/          # Operational module: facilities & branches
│   │   ├── payroll/                # Operational module: staff compensation
│   │   ├── analysis/               # Operational module: analytics & reporting
│   │   ├── wallet/                 # Operational module: payments & credits
│   │   ├── notification/           # Operational module: SMS/Email alerts
│   │   └── faq/                    # Operational module: workspace help & FAQs
│   ├── config/                     # Django settings, URLs, Celery & WSGI
│   ├── templates/                  # Django email and admin templates
│   ├── staticfiles/                # Static assets for WhiteNoise
│   ├── manage.py                   # Django CLI entrypoint
│   ├── requirements.txt            # Python dependencies
│   ├── Caddyfile                   # Reverse proxy configuration
│   ├── .env.example                # Environment variables template
│   ├── README.md                   # Detailed backend guide
│   ├── ARCHITECTURE.md             # System architecture & RBAC design
│   ├── API_DOCUMENTATION.md        # Complete REST API reference
│   └── OHM Backend API.zip         # Postman collection & environment bundle
└── README.md                       # Repository entry point (this file)
```

---

## Quickstart

### 1. Environment Setup
```bash
cd ohm_backend
cp .env.example .env
```

### 2. Run Database & Broker
```bash
docker compose up -d db redis
```

### 3. Run Migrations & Bootstrap Platform
```bash
python manage.py migrate_schemas --shared
python manage.py migrate_schemas
python manage.py bootstrap_platform \
  --email admin@ohm.com \
  --password admin \
  --full-name "OHM Super Admin" \
  --phone "+919999999999" \
  --title "Platform Administrator"
python manage.py create_default_groups
```

### 4. Run Services
```bash
# Terminal 1: Django API Server
python manage.py runserver 0.0.0.0:8000

# Terminal 2: Celery Worker
celery -A config worker -l info

# Terminal 3: Celery Beat Scheduler
celery -A config beat -l info
```

Check the health status:
```bash
curl http://localhost:8000/health/
# {"status": "ok", "schema": "public"}
```

For full details, proceed to **[ohm_backend/README.md](ohm_backend/README.md)**.
