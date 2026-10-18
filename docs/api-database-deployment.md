# API, database, and deployment guide

This describes the local/demo application, not a production service-level commitment. FastAPI's generated OpenAPI document is available at `/openapi.json`; Swagger UI is `/docs` and ReDoc is `/redoc` when the API is running.

## API surface (representative)

All protected routes require a valid bearer token. Role names below are enforced by the backend; the frontend's disabled/hidden controls are not the authorization boundary.

| Route | Purpose | Access / safety notes |
|---|---|---|
| `POST /api/v1/auth/login`, `GET /api/v1/auth/me`, `POST /api/v1/auth/logout`, `POST /api/v1/auth/refresh` | Local account login, persisted-session verification, per-session logout, and rotation | Login is public; remaining routes require bearer auth. The server stores only a SHA-256 fingerprint of each token `jti`; logout revokes that session, refresh revokes the old session and returns a new one. Sessions issued before the session migration require sign-in again. |
| `POST /api/v1/invoices/upload` | Validate, malware-scan, persist an upload receipt, and enqueue an invoice | Authenticated AP/admin route; supports validated PDF/image/DOCX inputs and `Idempotency-Key`; required scanner failure blocks intake, detections enter private quarantine without creating an invoice; returns saved invoice/job and dispatch state. No payment action. |
| `POST /api/v1/invoices/vendor/upload` | Upload an invoice for the authenticated vendor account | Vendor-only role, bound to one active vendor; uploads only. The local React portal uses this endpoint, preserves the account's vendor ID, and reuses the same idempotency key on safe retry. Vendor sessions do not fetch invoice/review/dashboard/document business data; API authorization also prohibits invoice listing/detail/document reads. Supports the shared validation/scan/idempotency path. No public registration or payment action. |
| `GET /api/v1/invoices/upload-status` | Read the durable result of an earlier upload using the original `Idempotency-Key` header | Authenticated internal/vendor role; scoped to the actor and current upload source/vendor. Returns `not_found`, `in_progress`, `abandoned`, `committed`, or `recovery_required` and actual receipt/invoice/job/outbox state where available. `Cache-Control: no-store`; no file bytes, mutation, retry, or dispatch. Unknown and out-of-scope keys are indistinguishable. |
| `POST /api/v1/ingestion/email` | Accept signed raw RFC822 mail and queue valid invoice attachments | Requires configured HMAC secret, exact sender allowlist, fresh timestamp, event ID, and signature over `timestamp.event_id.raw_body`. Bounds message/attachments, validates supported PDF/image/DOCX inputs, and deduplicates provider event ID plus body hash. No raw email body is stored. |
| `GET /api/v1/invoices`, `GET /api/v1/invoices/{id}` | Search/list and inspect invoice/evidence/job status | Authenticated; invoice detail includes latest outbox state/error class when a job exists. |
| `PATCH /api/v1/invoices/{id}/extraction` | Human field/line correction with reason | Reviewer/AP/admin route; versions extraction and writes feedback/audit evidence. |
| `GET/POST /api/v1/payment-proposals`, `GET /api/v1/payment-proposals/{id}` | List, inspect, or create an invoice-value payment proposal | Proposal creation is limited to admin/AP-clerk roles and eligible completed/matched or human-review-approved invoices with no unresolved review/fraud alert. Amount/currency/vendor are copied from invoice evidence; the API does not accept a different payment amount. |
| `POST /api/v1/payment-proposals/{id}/decision` | Approve or reject a proposal with a reason | Dedicated `approver` role only. The author, uploader, corrector, reviewer/assignee, or approver cannot approve the same proposal. Decision is audited and does not change invoice/payment state or execute a transaction. |
| `POST /api/v1/payment-proposals/{id}/cancel`, `POST /api/v1/payment-proposals/handoff` | Cancel an unhanded proposal or download a local human-review packet | Cancellation is reasoned/audited. Handoff is limited to a distinct AP clerk; JSON is marked `not_payment_instruction`, `executable: false`, and `payment_executed: false`, contains no bank data, and is not sent to ERP or a payment rail. |
| `GET /api/v1/review/feedback/training-export?limit=500&offset=0` | Download a page of approved correction pairs as JSONL | Admin-only; only feedback approved after a resolved review is included. Export is audited and does not train/promote a model; reviewer notes and source files are omitted. Invoice field values remain sensitive. |
| `POST /api/v1/invoices/{id}/reprocess` | Persist a new processing job and outbox event | Authenticated write role; never approves or pays the invoice. |
| `GET /api/v1/admin/processing-outbox?state=pending` | Read outbox counts and recent dispatch events | Admin only. Error detail is limited to a safe exception type. |
| `POST /api/v1/admin/processing-outbox/{event_id}/retry` | Explicitly retry an unpublished event | Admin only; body requires `{"reason":"..."}` (8–500 chars). Audited; rejects published, actively claimed, missing, and non-dispatch-failed processing jobs. Does not approve/pay. |
| `GET /api/v1/admin/processing-jobs/stale`, `POST /api/v1/admin/processing-jobs/{job_id}/recover` | Find and explicitly mark stale work failed for human review | Admin only; reason and expected state required. This endpoint does not retry automatically. |
| `GET /api/v1/admin/storage-integrity` | Bounded read-only comparison of DB document paths and disk files | Admin only; reports samples, never adopts/deletes/repairs files. |
| `GET /api/v1/admin/quarantined-uploads` | List quarantined attachment metadata and storage presence | Admin only; no quarantine bytes/filesystem paths; `download_available: false`; no release route. |
| `GET /api/v1/admin/upload-recovery`, `POST /api/v1/admin/upload-recovery/run` | Inspect and explicitly run stale upload receipt recovery | Admin only; list omits storage paths/idempotency keys; recovery records an audit event, preserves committed documents and may remove only eligible stale unreferenced files. |
| `GET /api/v1/health/live`, `/api/v1/health/ready`, `/api/v1/health/worker` | Liveness, dependency readiness, and Celery worker checks | Readiness checks DB/storage/quarantine plus conditional Redis/ClamAV/OCR. Worker endpoint is separate from API readiness. |
| `/api/v1/vendors`, `/api/v1/purchase-orders`, `/api/v1/goods-receipts` | Local/manual procurement reference records | Usable without an external provider. |
| `POST /api/v1/admin/integrations/erp/sync` | Explicit, admin-triggered, REST sandbox import of vendor, PO, and GRN reference records | Requires both `ERP_SANDBOX_BASE_URL` and `ERP_SANDBOX_TOKEN`; missing credentials return 503 before network access. Conflicts with manually managed records abort and roll back the local sync. It does not submit invoice status/approval or initiate payments. |
| `GET /api/v1/admin/integrations/erp/invoices/{invoice_id}/status` | Read one provider-reported invoice status using local invoice number/vendor code | Admin-only; requires configured credentials; audits lookup only. Provider status is not persisted, does not change local workflow, and never authorizes payment. |

Most errors use FastAPI's `{"detail": ...}` shape. Responses include a request ID; the client carries it in `X-Request-ID`. The API's role policy and current schema are authoritative if this table and generated OpenAPI differ.

## Database and migrations

SQLAlchemy models are in `backend/app/models.py`. The checked-in Alembic chain is:

1. `07f36e21fa3d` — initial AP, procurement, review, audit, and screening tables.
2. `2ea3f64b93bd` — nullable processing-job `heartbeat_at`.
3. `65e08a82bd31` — durable `processing_outbox`, indexes, uniqueness, and a one-time backfill of pre-existing queued jobs.
4. `d170fe32a6b1` — nullable external source/ID pairs and uniqueness/check constraints on vendor, purchase-order, and goods-receipt records. Existing local rows remain unowned (`NULL`/`NULL`); no user data is deleted or rewritten.
5. `0e7a2cb91fd4` — email ingestion event idempotency metadata, without storing raw email bodies.
6. `8d2f43b1c76a` — quarantine metadata for malware-positive uploads and email attachments.
7. `4f0c9d2e6a31` — upload receipt state for crash recovery and idempotent repair.
8. `7b4a90c1d6e2` — nullable vendor binding on users, the vendor role, and an additive index/FK for upload-only supplier accounts. Downgrade refuses while vendor-bound users or role assignments exist rather than deleting those accounts.
9. `c6d2e0a1f7b4` — `auth_sessions` stores the user, SHA-256 token-identifier fingerprint, expiry, creation and revocation timestamps. It does not store a bearer token or raw `jti`; protected requests require an active matching row. The migration adds a table and does not rewrite user/vendor rows.
10. `a71f3c2e9b40` — adds `payment_proposals`, invoice/version uniqueness, positive-amount/status checks, restrictive invoice/vendor/review-decision references, actor metadata, invoice-evidence fingerprint, and an `approver` role. Downgrade refuses while proposal records or approver assignments exist to avoid data loss. This is the current source head.

The outbox has a foreign-keyed invoice/job, optional actor, event type, `pending`/`published`/`dead` state, bounded attempt counter, next-attempt time, claim lease/token, publish timestamp, safe error class, and creation timestamp. `(job_id, event_type)` is unique, so one processing job cannot accidentally gain multiple dispatch intents. Uploads first persist an `UploadReceipt` intent, then stage/promote file bytes and commit invoice/document/job/outbox/audit/idempotency with the receipt marked committed. The filesystem and SQL transaction are still separate durability domains; bounded startup/admin receipt recovery handles known stale states, but process-kill/power-loss at every boundary has not been verified.

`QuarantinedUpload` stores source/reference metadata, SHA-256, scanner/detection fields, status, and a private quarantine path; the bytes are held separately from invoice documents. The admin route reports only metadata and storage presence, exposes no bytes or path, and has no automated release. Quarantine database metadata is persisted before file bytes; an idempotent retry repairs a missing/partial file. Simulated file-write failure/retry is tested; a live ClamAV daemon/image is not.

Run a migration as an explicit release step, against a configured database URL:

```bash
cd backend
export DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST:5432/DB'
alembic upgrade head
```

Do not point an experiment at a database with user data. Back up and rehearse migrations first. Upgrade preserves existing rows; the outbox revision backfills queued jobs only, not completed/failed jobs. Its downgrade drops the outbox table and therefore discards event history/queued dispatch intent—use only as a deliberate recovery plan. The current workspace DB `backend/invoice_fraud.db` remains at `07f36e21fa3d` and has not received the quarantine, upload-receipt, vendor-binding, auth-session, or payment-proposal migrations; it was deliberately not modified. Disposable SQLite verification and the migration-preservation regression now upgrade through head `a71f3c2e9b40`. The synthetic regression verifies a pre-existing account, role assignment, and vendor survive migration; the latest migration adds the proposal table and dedicated role without rewriting those rows. This does not test the existing workspace DB or PostgreSQL. The CI workflow configures a PostgreSQL preservation check against a separate disposable database, but that job has not run; live PostgreSQL behavior remains unverified. In local `AUTO_CREATE_SCHEMA=true` mode, the startup compatibility helper creates metadata without dropping tables, adds missing additive tables (including `auth_sessions` and `payment_proposals`) and nullable heartbeat, ERP identity, and user-to-vendor binding columns/indexes to older SQLite demo schemas when needed, ensures the vendor and approver roles, and backfills queued jobs missing an outbox row. These local compatibility changes are additive and preserve existing accounts/rows; preserve and back up the workspace DB before rehearsal. Production must keep `AUTO_CREATE_SCHEMA=false` and use reviewed Alembic migrations.

The ERP identity migration keeps nullable `(external_source, external_id)` pairs so repeat syncs can update only records previously imported from the same sandbox. Local/manual rows are never silently overwritten: a natural-key collision is linked only when its data matches exactly; otherwise the sync returns a conflict and rolls back the complete database transaction. A disposable SQLite upgrade from the previous head preserved synthetic vendor/PO/GRN rows and left both new identity columns null. Production PostgreSQL migration and concurrent-sync behavior remain unverified.

Cumulative PO-quantity matching reads the latest stored `InvoiceMatchResult.details.line_results` for prior invoices on the same PO, excludes rejected/cancelled/voided invoices, and sums by PO line number. It adds no database column. Because this is based on stored prior match evidence, validate/correct prior invoice dispositions before relying on a cumulative exception; this is a review signal, not a payment gate.

## Dispatch and recovery semantics

- A pending outbox event is claimed with a UUID fencing token and a 120-second lease. Competing dispatchers use conditional database updates; PostgreSQL row locking and SQLite conditional updates are separate behaviors, and only SQLite behavior has been exercised here.
- Dispatch failure leaves the job queued and the invoice/source saved. The outbox retries up to eight claimed attempts with exponential delays (2, 4, 8, 16, 32, 64, and 128 seconds before the final attempt); exception messages, broker URLs, and credentials are not stored/logged. If the last claim is lost to a process crash, an expired lease at the attempt limit is finalized as dead rather than allowing unbounded ninth attempts.
- On exhaustion the job is marked failed at stage `dispatch`, the invoice goes to human review, a review case and audit event are written, and the outbox event becomes `dead`. An admin can retry with a recorded reason after checking the broker/worker; retry resets the attempt budget. A published event is not manually resent through this route.
- Delivery is **at least once**. A publisher may accept a message and lose its acknowledgement, so the outbox may re-dispatch after a lease expires. Processing-job claims fence duplicate workers; duplicate publication is not proof of duplicate processing. `published` means the message was handed to the configured dispatcher/broker, not that OCR completed or the invoice was approved.
- Idempotent upload replay returns the same saved invoice/job. It does not change the idempotency key, skip a pending backoff, or republish an already-published event. If an admin retry is needed, use the outbox route rather than submitting a new upload.
- The invoice drawer shows dispatch state; the admin screen lists counts/events and offers the audited retry. Neither status enables or triggers payment.

## Local development and optional services

### SQLite / local background mode

```bash
cd backend
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

By default, local development uses SQLite, schema auto-creation, synthetic demo seed data, and `TASK_MODE=background`. The API startup loop polls the durable outbox; dispatch and document processing in this mode run inside application background work. It is convenient for a demo, not a scalable worker fleet. The document root is `STORAGE_DIR`; protect and back it up with the database.

### Celery / Redis mode

Configure `TASK_MODE=celery`, `REDIS_URL`, `DATABASE_URL`, and the shared `STORAGE_DIR`. Run the API, an outbox dispatcher, and a Celery worker with the same environment and database/storage:

```bash
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
python -m app.outbox_dispatcher --interval 5 --batch-size 25
celery -A app.tasks.celery_app worker --loglevel=INFO
```

The API includes a recovery poller and a standalone dispatcher can be run as a separate service. Multiple dispatchers are intended to be safe under conditional claims, but live multi-process, PostgreSQL, Redis, Celery acknowledgement-loss, and Docker recovery have **not** been tested in this workspace. Never interpret configuration or unit tests as proof of a running broker/worker.

### Compose

`docker-compose.yml` supplies PostgreSQL, Redis, ClamAV, a one-shot Alembic migration service, API, Celery worker, outbox dispatcher, and web. Copy `.env.example` to `.env`, replace the `SECRET_KEY` and `POSTGRES_PASSWORD` placeholders with unique random values, then use `docker compose up --build`; Compose now fails closed if either required value is unset. Docker/Compose was unavailable in this authoring environment, so container builds, health checks, ClamAV startup, volume persistence, port exposure, and worker communication are unverified. CI configures Python dependency/source-secret scans and high/critical API, frontend, PostgreSQL, Redis, and ClamAV image scans; these container scans have not run. Compose is not a production profile. The selected target is a single-organization private demo; no organization/tenant isolation is implemented. Before any hosted use, review private UI/API access controls, `APP_ENV`, TLS/reverse-proxy policy, secret injection, database backups, storage/quarantine permissions, logs/retention, rate limits, and identity controls. Do not onboard multiple organizations.

## Configuration and credentials

| Variable | Required? | Notes |
|---|---|---|
| `SECRET_KEY` | Required by Compose; local application has a demo-only fallback | Strong random signing key; production startup rejects the built-in development default. Compose fails if unset. Store in a secret manager for shared deployments; never commit populated `.env`. |
| `DATABASE_URL` | Required | Local SQLite default is provided; Compose derives its PostgreSQL URL from `POSTGRES_PASSWORD` unless an explicit URL is set. Use least-privilege credentials. |
| `POSTGRES_PASSWORD` | Required by Compose | Provide a unique random secret through `.env` for local demo or a secret store in deployment; Compose fails if unset. CI creates a fresh masked run-scoped value. |
| `REDIS_URL` | Celery mode | Required to reach Redis; no broker credential is supplied by this repository. |
| `STORAGE_DIR` | Configurable | Writable protected path shared by API and workers; use persistent restricted storage. |
| `QUARANTINE_DIR` | Configurable | Separate private writable path/volume for infected attachments; do not serve it through the web or document API. |
| `MALWARE_SCAN_MODE`, `MALWARE_SCAN_REQUIRED` | Optional | `off` is demo-only; use `clamd` plus `MALWARE_SCAN_REQUIRED=true` when untrusted documents may be uploaded. Required scanner errors block intake. |
| `CLAMAV_HOST`, `CLAMAV_PORT`, `CLAMAV_TIMEOUT_SECONDS`, `CLAMAV_MAX_STREAM_MB` | Required when scanning | ClamAV daemon address and bounded TCP scan settings. Validate live engine/signature update behavior in the target deployment; Compose wiring is not runtime-verified. |
| `UPLOAD_RECOVERY_GRACE_SECONDS`, `UPLOAD_RECOVERY_BATCH_SIZE` | Optional | Bounds stale upload receipt cleanup; recovery is admin-audited, retains committed document bytes, and may remove only stale unreferenced staging/final files. |
| `TASK_MODE` | Optional | `background` by default locally; use `celery` for broker-backed dispatch. |
| `AUTO_CREATE_SCHEMA`, `DEMO_SEED` | Optional | Local demo defaults true; set both false for migration-first deployment. |
| `LAYOUT_MODEL_PATH` | Optional | Path to an approved local fine-tuned token-classification checkpoint. Nothing is downloaded automatically; no model artifact is bundled. |
| `LAYOUT_MODEL_DEVICE`, `LAYOUT_MODEL_MIN_CONFIDENCE` | Optional | Default CPU and 0.75 mean raw softmax score threshold. The softmax is uncalibrated; accepted model candidates force human validation. |
| `DUPLICATE_EMBEDDING_MODEL_PATH` | Optional | Path to an approved local sentence-transformer checkpoint. Install `backend/requirements-embeddings.txt` separately; the app sets local-only loading and never downloads weights. Without a checkpoint, deterministic duplicate rules remain active. |
| `DUPLICATE_EMBEDDING_SIMILARITY_THRESHOLD` | Optional | Default 0.92 cosine cutoff. This is an uncalibrated investigation threshold, not a probability or validated operating point. |
| `EMAIL_INGEST_WEBHOOK_SECRET`, `EMAIL_INGEST_ALLOWED_SENDERS` | Optional; both required for webhook | Use a random secret of at least 32 characters and comma-separated exact sender addresses. Configure only behind a trusted ingress that verifies provider authenticity/DMARC and signs the full RFC822 payload. Missing values block before reading the body. |
| `EMAIL_INGEST_MAX_MESSAGE_MB`, `EMAIL_INGEST_SIGNATURE_TOLERANCE_SECONDS` | Optional | Defaults 25 MB and 300 seconds; hard clamps cap message size to 50 MB and signature age to 15 minutes. Per message: at most five attachments and combined attachment bytes up to `MAX_UPLOAD_MB`. |
| `OCR_TIMEOUT_SECONDS`, `OCR_DOCUMENT_TIMEOUT_SECONDS` | Optional | Default 20 seconds per Tesseract invocation and 300 seconds across OCR work for one document. Timed-out documents fail safely and enter human review; preprocessing CPU time is not separately process-killed. |
| `ERP_SANDBOX_BASE_URL`, `ERP_SANDBOX_TOKEN` | Optional; both required to sync | API-host environment only. HTTPS required except loopback test/mock servers. Token is sent as a bearer credential only to the configured HTTPS base; it is not returned/logged. Missing either blocks before network access. |
| `ERP_SANDBOX_TIMEOUT_SECONDS` | Optional | Default 15 seconds, clamped to 1–60 seconds per HTTP request; total sync wall time is capped at 120 seconds. The sync is bounded to 2,000 records/resource and 2 MB/response page. |

No email provider, mailbox, cloud OCR, SSO, bank-validation, payment, or blockchain credentials are configured. The generic signed RFC822 receiver is currently disabled because its HMAC secret and sender allowlist are unset. Configure those only after placing the endpoint behind an authorized trusted ingress that verifies upstream sender authenticity; the application cannot verify that deployment prerequisite itself. The ERP sandbox adapter is also optional and unconfigured by default. Do not place document samples or secrets in third-party tools without explicit authorization.

## Optional REST ERP sandbox contract (reference data only)

When explicitly configured, the connector uses only HTTP `GET` requests to the following relative paths and requires a JSON page with `items` and optional `next_cursor`:

- `GET api/v1/vendors?limit=100&cursor=...`
- `GET api/v1/purchase-orders?limit=100&cursor=...`
- `GET api/v1/goods-receipts?limit=100&cursor=...`
- `GET api/v1/invoice-status?invoice_number=...&vendor_code=...` (one provider status record at most)

Each reference record requires a stable non-sensitive `external_id`. Vendor records carry `code`, `name`, optional `tax_identifier` and `aliases`; PO records carry `number`, `vendor_external_id`, `currency`, `status`, ISO `issue_date`, and numbered `lines`; goods receipts carry `number`, `purchase_order_external_id`, ISO `received_date`, and lines optionally keyed by `purchase_order_line_number`. Status lookup returns a provider-reported label only; it is not persisted or used to change local workflow. See `backend/app/integrations/erp_sandbox.py` for strict field limits and validation rules. Each reference resource is bounded to 2,000 records and 2 MB per page; redirects are not followed; only HTTPS is accepted except for a loopback mock server. Provider response bodies and credentials are not logged or returned to the client.

Set the following only on the API host (do not expose them as `VITE_*` frontend variables):

```bash
ERP_SANDBOX_BASE_URL=https://your-authorized-sandbox.example
ERP_SANDBOX_TOKEN=... # inject with your secret manager; never commit
ERP_SANDBOX_TIMEOUT_SECONDS=15
```

The UI control calls the admin-only route. No request is made unless both URL and token pass validation. Sync writes to local reference tables transactionally, records source identities, and audits counts only. Conflicting manual data aborts the entire sync; already-imported POs with linked invoices/receipts cannot be changed by this path, and imported receipts are immutable. A separate admin-only status route sends only the invoice number and vendor code as query parameters and can read one provider status; no invoice file is transmitted. The result is not persisted and never writes approval/payment state locally or to an ERP. The connector does not execute payments. The tested server is an in-process HTTPX mock transport—not a vendor sandbox, network integration, or production ERP. Provider-specific authentication/pagination, reconciliation, retry policy, and credentials still require authorization and contract testing.

## Signed email attachment ingress (generic webhook, not a mailbox integration)

`POST /api/v1/ingestion/email` accepts a raw RFC822 message from a trusted server-side ingress. It is disabled unless both `EMAIL_INGEST_WEBHOOK_SECRET` (at least 32 characters) and an exact `EMAIL_INGEST_ALLOWED_SENDERS` list are configured. There is no mailbox polling, provider account, or webhook-provider-specific verification in this repository. The organization-selected ingress must verify the provider's signature and sender policy/DMARC before applying this application's HMAC.

The ingress computes an HMAC-SHA256 over `ASCII(timestamp) + b"." + ASCII(event_id) + b"." + raw_message` using the configured secret. Send these headers: `X-InvoiceGuard-Event-ID`, `X-InvoiceGuard-Timestamp` (Unix seconds), and `X-InvoiceGuard-Signature` (`sha256=<hex>`). Timestamps older than the configured tolerance are rejected. Example signing code:

```python
import hashlib, hmac, time
stamp = str(int(time.time()))
message = open("message.eml", "rb").read()
event_id = "provider-event-id"
signature = hmac.new(secret.encode(), stamp.encode() + b"." + event_id.encode() + b"." + message, hashlib.sha256).hexdigest()
```

The API bounds raw message and attachment sizes/count, checks the parsed `From` against the configured exact allowlist, accepts only supported file signatures, parses PDF page count/encryption and image dimensions, and validates DOCX through the bounded OOXML reader. It creates email-sourced invoice/job/outbox records in one DB transaction and stores only event ID, content hash, counts, and response metadata—not the email body or sender address. Replaying the same event ID and same body returns the existing invoice IDs without creating a duplicate; reusing an ID with different content returns 409. An invalid attachment rejects the email event before persistence. A host crash between filesystem write and DB commit can still leave an orphan file; use the admin read-only storage-integrity report for investigation—there is no auto-delete/adopt behavior.

These tests use synthetic MIME messages and the local API only. No actual email provider or mailbox was connected. Apply upstream provider authentication, DMARC/SPF/DKIM policy, ingress rate limits, IP/network restrictions where feasible, retention, and security review before exposing the route.

## Approved correction export (controlled training-data preparation only)

`GET /api/v1/review/feedback/training-export?limit=500&offset=0` is admin-only and exports only `ModelFeedback` rows explicitly approved after a resolved human review. The output is JSONL containing the prediction, corrected field values, random sample/invoice IDs, and review timestamp; it omits reviewer notes and document bytes. The response is `no-store`, export counts/offset are audit logged, and pages are bounded to 500 records. `X-Feedback-Export-Total` identifies the full approved count; request subsequent pages with `offset=500`, etc. Treat the output as sensitive supplier data. Export does not create labels for uncorrected fields, train a model, promote a checkpoint, or establish accuracy.

## Optional local sentence-embedding duplicate candidate scorer

The deterministic duplicate rules remain active by default. If `DUPLICATE_EMBEDDING_MODEL_PATH` points to an approved local sentence-transformer model and the optional package in `backend/requirements-embeddings.txt` is installed, the duplicate service can add cosine similarity of invoice number/PO/currency/line descriptions as an investigation feature. Model loading requires a local directory, `local_files_only=True`, and `trust_remote_code=False`; it never downloads weights. Missing model/package/inference failure falls back to existing exact hash, normalized identifier, fuzzy/line logic. The default cosine cutoff of 0.92 is an uncalibrated review threshold; no checkpoint, held-out benchmark, semantic accuracy, or measured duplicate performance is supplied.

## Optional local layout model (not a pretrained product feature)

An opt-in LayoutLM-compatible local token-classification adapter is present in `backend/app/services/layout_model.py`. It consumes OCR/PDF word boxes, loads only an administrator-configured local checkpoint with `local_files_only=True` and `trust_remote_code=False`, and maps a small set of invoice-field BIO labels. It is disabled unless `LAYOUT_MODEL_PATH` is set. To install its separate runtime, use `pip install -r requirements-layout-model.txt` only after reviewing the large PyTorch/Transformers dependency footprint; the minimum versions were raised after the dependency audit identified vulnerable Transformers 4.x and PyTorch 2.6.0 releases. Re-run the optional dependency audit and validate API compatibility before supplying a vetted checkpoint containing meaningful invoice entity labels. No suitable labeled corpus, fine-tuned checkpoint, pretrained capability claim, accuracy metric, or model download is provided here. A confidence threshold does not convert raw softmax scores into calibrated accuracy. Candidate fields retain deterministic evidence, add a model-source note, and require human validation; load/inference errors preserve the deterministic extraction and surface a validation warning.

## Verification and limitations

See `docs/traceability-matrix.md`, `docs/status-matrix.md`, `docs/security-operations.md`, `docs/evaluation.md`, and `docs/changelog.md`. Latest clean verification on 2026-10-17 with `REQUIRE_TESSERACT=1` passed **147 backend tests, 0 skipped, 3 warnings; 43 frontend tests; production build; `pip check`; clean `npm ci`; and npm audit (0 vulnerabilities)**. Tesseract 5.5.0 with `eng`/`osd` data was present and OCR integration ran on synthetic fixtures only; no representative accuracy result is claimed. Backend JUnit XML and required-Tesseract preflight/log are under `docs/verification/evidence-2026-10-17/`. Prior strict `pip-audit` reports for unchanged core/optional manifests remain under `docs/verification/evidence-2026-10-16/` and cover resolver-selected versions only. The former Transformers 4.x range was tightened after it resolved nine advisories. Trivy source-secret and API/frontend/PostgreSQL/Redis/ClamAV image scans are configured in CI but have not run; no image CVE result is claimed. Alembic reached head `a71f3c2e9b40` on disposable SQLite; same-PO allocation concurrency passed on file-backed SQLite and PostgreSQL lock SQL compilation includes `FOR UPDATE`, but a live PostgreSQL run is unobserved. Chromium Playwright passed 8/8, including U03 upload recovery and U01's separated proposal approval/local non-executable handoff. No hosted GitHub/Render steps, Docker runtime, PostgreSQL deployment, live Redis/Celery broker, ClamAV engine, or provider contract is evidenced. The deployment scope is one organization and private access; no tenant isolation is implemented or claimed. The owner can provide authorized, de-identified benchmark data, but none is in the workspace yet. Do not claim production readiness, model accuracy, fraud losses prevented, ROI, or autonomous approval.
