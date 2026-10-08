# OHM multi-tenant architecture

OHM is the platform for healthcare service providers such as Vaishnavi Medicare and Sai Clinic. Each provider gets an isolated tenant schema. OHM manages provider workspaces; each provider manages the people and operations inside its own workspace.

## Ownership boundary

| Area | Owner | Data location |
| --- | --- | --- |
| Provider workspace, status, enabled modules, verified domains, subscriptions, features, platform admin profiles, provisioning requests | OHM root users | `public` schema (`Tenant`, `Domain`, `Subscription`, `Feature`, `PlatformAdmin`, `TenantProvisioning`) |
| Root accounts and platform administration | OHM | `public` schema (`accounts.User`, `PlatformAdmin`) |
| Provider admin, employee accounts, customer accounts | Provider | That provider's tenant schema |
| Booking, invoices, payments, and other service records | Provider | That provider's tenant schema, when those modules are added |

`Tenant` is OHM's platform record for one service provider. The `schema_name` on that row identifies the provider's isolated database schema. It is metadata about the provider, not a shared container for the provider's operational records.

## Request routing

React sends requests to the single API host and identifies the selected workspace with `X-Tenant`:

```text
React (ohm.com or a tenant subdomain)
                 │
                 ▼
             api.ohm.com
                 │
        X-Tenant: vaishnavi-medicare
                 │
                 ▼
      tenant lookup and schema switch
                 │
         Django REST Framework
                 │
          django-tenants
       ┌─────────┼─────────┐
       ▼         ▼         ▼
     public   tenant 1   tenant 2
```

On `api.ohm.com`, a request without `X-Tenant` stays in the `public` schema. A request with the header resolves the
slug to a provisioned tenant, requires an active workspace with a verified domain, then switches the database
connection to that tenant's schema before URL resolution. Tenant JWTs carry the schema name, and authentication
rejects a token used with a different `X-Tenant` value. Direct verified tenant domains continue to resolve by host.
Only authenticated root superusers can manage tenants through `/api/platform/tenants/`.

The public platform tenant is infrastructure for OHM itself. It is excluded from tenant CRUD and must never be provisioned, suspended, or deleted as a provider workspace. `bootstrap_platform` registers `OHM_API_HOST` as a public-schema domain.

## Provisioning a provider

Applicants submit and update onboarding requests in the public schema. A private request token authorizes the applicant to read or edit their request while it is submitted or waiting for more information. The initial admin password is stored only as a Django password hash and cleared after successful provisioning or rejection.

An OHM root admin reviews the request, can ask for more information, and selects which requested modules are approved by patching the same request resource applicants use. Creating a tenant from an approved request provisions the provider schema, registers domains, seeds tenant roles, and creates the first Tenant Admin. The management command `provision_tenant <request-uuid>` invokes that same approved-request flow; it cannot provision a pending request. Root users are created separately through Django admin or `bootstrap_platform`.

## Adding provider features later

Keep tenant-owned features in their own Django apps, such as `employees`, `customers`, `booking`, `invoices`, and `payments`. Add those apps to `TENANT_APPS` only, mount their endpoints in the tenant URL configuration, and enforce tenant roles and module access there. Do not put provider operational records in the public schema or in the platform's `Tenant` model. Django-tenants currently migrates every configured tenant app into each provider schema; approved modules are feature-access settings, not a per-tenant migration selection mechanism.

The shared `accounts.User` model authenticates both platform and tenant logins; it is installed in public and tenant schemas. Public platform administrators have a `PlatformAdmin` profile. Tenant `accounts` also contains `Role`, `Permission`, and `EmployeeProfile`. The existing `accounts.User` represents a login identity. A future customer domain model should represent the provider's customer record and can be linked to a login account when needed; the two concepts should not be conflated.
