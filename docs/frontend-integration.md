# Frontend integration

How each screen of the [web app](https://github.com/glalit52/anvita-sales-marketing-agent/tree/main/apps/web)
maps to the API, and where the API's shapes differ from `apps/web/src/lib/mock/*`.

## Conventions

* **Base URL:** `${API_URL}/api/v1`. Send `Authorization: Bearer <accessToken>`.
* **Session:** `POST /auth/login` returns `{accessToken, refreshToken, expiresIn, user, org, orgs}`. Access
  tokens last 15 minutes. On a `401` with `error.code = "token_expired"`, call `POST /auth/refresh` once and
  retry. Refresh tokens rotate on every use, and replaying an old one signs the user out everywhere.
* **Casing:** responses are camelCase, like the mocks. Query strings may be camelCase too (`?contactId=`).
* **Ids** are UUIDs (the mocks use `c1`, `v1` and so on).
* **Money** is `…Minor` integers in paise (`valueMinor: 1200000` is ₹12,000). The mocks use rupees.
* **Time** is ISO 8601 UTC. Relative labels ("2m ago") are computed in the client from `lastActivityAt`,
  `lastMessageAt` or `createdAt`.
* **Pagination:** list endpoints return `{items, total, limit, offset}`.
* **AI ownership:** `ownerId: "ai"` means the AI owns the contact, as in the mocks. Send `ownerId: null` to
  give it back to the AI.

## Screen by screen

| Screen (route) | Mock source | API |
|---|---|---|
| Login, onboarding (`/login`, `/onboarding`) | – | `POST /auth/signup`, `/auth/login`, `/auth/accept-invite`; then `PATCH /org`, `POST /locations`, `POST /agent/knowledge` (website import) |
| Home (`/dashboard`) | `core.dailyTrend`, KPIs | `GET /dashboard?days=30`, `GET /tasks?group=today`, `GET /agent/stats` |
| Inbox (`/inbox`) | `core.conversations` | `GET /conversations?view=…&status=…&channel=…&q=…`, `GET /conversations/counts`, `GET /conversations/{id}` (includes `messages` and `contact`), `POST /{id}/messages` (`isNote` for internal notes), `POST /{id}/read`, `PATCH /{id}` (assign, close, `assigneeType: "ai"` to hand back), `POST /{id}/drafts/{mid}/send` (Copilot approve) |
| Tasks (`/tasks`) | `app.tasks` | `GET /tasks?group=overdue|today|upcoming|done&assignee=me`, `POST/PATCH/DELETE /tasks` (`done: true` completes a task) |
| AI Agent (`/agent`) | `platform.*` | `/agent/config`, `/agent/knowledge` (+ `/faqs`, `/search`, `/{id}/resync`), `/agent/playbooks`, `/agent/escalation-rules`, `/agent/sandbox`, `/agent/publish`, `/agent/versions`, `/agent/runs` |
| Contacts (`/contacts`, `/contacts/[id]`) | `sell.allContacts`, `timelineFor`, `consentFor` | `GET /contacts?q&stage&tag&owner&sort`, `POST /contacts/bulk`, `GET /contacts/{id}/timeline`, `/notes`, `/consents`, `/conversations` |
| Pipeline (`/pipeline`) | `sell.deals` | `GET /deals?pipeline=patients|b2b`, `GET /deals/summary`, `PATCH /deals/{id}` with `stage` (drag between columns) |
| Calendar (`/calendar`) | `sell.appointments`, `services`, `providers` | `GET /appointments?start&end&locationId`, `GET /availability`, `POST /appointments`, `PATCH /appointments/{id}` (reschedule, status), `/services`, `/providers`, `/locations` |
| Payments (`/payments`) | `sell.payments` | `GET /payments`, `GET /payments/summary`, `POST /payments` (amount or invoice lines), `POST /{id}/mark-paid|cancel|refund` |
| Notifications (`/notifications`) | `app.notifications` | `GET /notifications?tab=…`, `GET /notifications/unread-count`, `POST /notifications/read` |
| Settings (`/settings`) | `platform.*` | `/org`, `/locations`, `/team`, `/invites`, `/channels`, `/audit-log` |
| Booking page (`/book/[slug]`) | `public.business`, `slotsFor` | `GET /public/orgs/{slug}`, `GET /public/orgs/{slug}/availability`, `POST /public/orgs/{slug}/bookings` |
| Pay page (`/pay/[id]`) | `public.payLinks` | `GET /public/pay/{token}`. In test mode, `POST /public/pay/{token}/test-confirm`. With Razorpay, redirect to the provider checkout. |
| Webchat widget | `components/public/webchat-widget.tsx` | `POST /public/webchat/{slug}/sessions` → widget token; `POST /public/webchat/messages` (send `clientId` so retries de-dupe); poll `GET /public/webchat/messages?after=<createdAt>` |
| Reviews (`/reviews`) | `growth.reviews`, stats | `GET /reviews?platform&status&rating`, `GET /reviews/stats`, `POST /reviews/{id}/draft|reply`, `GET/PUT /reviews/settings`, `GET /review-requests`, `GET /review-requests/funnel`, `POST /review-requests` |
| Feedback page (`/feedback/[id]`) | `public` | `GET /public/feedback/{token}`, `POST /rate`, `POST /private`, `POST /posted` |
| Campaigns (`/campaigns`) | `outbound.campaigns`, `waTemplates`, `aiSegments` | `/segments` (+ `/preview`), `/templates`, `/campaigns` (+ `/launch`, `/cancel`, `/audience`) |
| Sequences (`/sequences`) | `outbound.sequences` | `/sequences`, `POST /{id}/status?status=active|paused`, `/{id}/enroll`, `/{id}/enrollments`, `/sequences-meta/mailboxes` |
| Prospecting (`/prospecting`) | `outbound.*` | `POST /prospecting/plan` → `POST /prospecting/searches` (202, runs in the worker; poll the search) → `GET /searches/{id}/rows`, `POST /enrich`, `POST /columns`, `POST /add-to-crm` |
| Marketing (`/marketing`) | `growth.*`, `demand-gen.*` | `/marketing/ads/*`, `/marketing/content`, `/marketing/social`, `/marketing/landing-pages`, `/marketing/aeo/*`, `/marketing/campaigns`, `/marketing/approvals` |
| Automations (`/automations`) | `platform.workflows` | `/automations`, `/automations/meta`, `/automations/runs`, `/{id}/toggle?enabled=`, `/from-template/{id}` |
| Analytics (`/analytics`) | `growth.overviewKpis`, … | `/analytics/overview?range=`, `/revenue-by-source`, `/revenue-weekly`, `/attribution?model=`, `/funnel`, `/ai`, `/team`, `/outbound` |
| Integrations, billing, API (`/integrations`, `/settings`) | `platform.*` | `/integrations` (+ `/connect`, `/disconnect`), `/billing`, `/api-keys`, `/webhook-endpoints`, `/data-requests`, `/me/notification-preferences`, `/booking-settings` |

## Paid APIs

Screens that need a paid data source (Apollo, Google Places, ad platforms, Google Business Profile, answer
engines, social publishing) call a provider slot. In development, sample providers return deterministic
data so every screen works; in production an unconfigured slot answers `501 provider_not_configured` with
`details.options` listing the vendors and env vars. Show that as a "Connect X" prompt rather than an error.
