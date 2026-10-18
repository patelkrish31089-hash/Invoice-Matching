# Architecture and implementation status

## Request and processing path

```text
React / Vite or nginx
          │ same-origin /api proxy
          ▼
FastAPI ── SQLAlchemy ── SQLite (local) / PostgreSQL (compose)
   │             │
   │             └── Alembic schema history
   ├── protected source storage (UUID path + SHA-256)
   ├── PyMuPDF text layer / Tesseract CPU OCR
   ├── deterministic extraction + bounded local Tesseract OCR + optional local fine-tuned token-classifier adapter
   ├── Decimal matching + line-aware duplicate signals + explainable risk signals + human review
   ├── signed raw-RFC822 email attachment webhook (optional; provider ingress not connected)
   ├── optional admin-triggered REST ERP sandbox reference sync (vendor / PO / GRN only)
   ├── transactional processing outbox ── local poller or Celery + Redis
   └── append-only hash-linked audit events
```

## Implemented

- FastAPI routes for login/session, invoices, secure upload/download, extraction correction/versioning, review cases, non-executable payment proposals, fraud alerts, reference vendors, purchase orders, goods receipts, settings, health, CSV/PDF reports, and model-version records.
- Optional admin-only REST ERP sandbox integration with bounded pagination/response sizes, strict record validation, environment-only bearer credentials, HTTPS/loopback-mock restrictions, source identities, idempotent transactional vendor/PO/GRN upserts, manual-data collision protection, and audit counts. A separate one-invoice status lookup is read-only and does not persist the status or change local workflow. No approval/payment is submitted. Verified with local HTTPX mock transport, not an external ERP.
- Optional generic email attachment ingress at `POST /api/v1/ingestion/email` accepts raw RFC822 only with a configured HMAC secret, exact sender allowlist, fresh timestamp, bounded/parsed attachments, and provider-event/content idempotency; it creates invoice/job/outbox/audit records but does not store the raw email. Synthetic MIME/API tests pass. No mailbox or provider-specific webhook is connected; upstream sender authentication/DMARC and trusted ingress remain deployment prerequisites.
- PBKDF2 password hashes and signed expiring bearer tokens; route-level role checks for admin, AP clerk, reviewer, dedicated proposal approver, auditor, and vendor uploader. The demo has no corporate identity-provider integration. In Vite development, a dev-only alternate auth header is normalized by the `/api` proxy for hosted-preview compatibility; FastAPI still enforces bearer auth, and auth rejection logs include request ID/path/reason without secrets.
- Upload content/signature checks, file-size cap, UUID storage naming, SHA-256 duplicate candidate metadata, idempotency header, authenticated document access, and page-to-PNG preview used for extraction evidence.
- Durable `ProcessingOutbox` dispatch intent written transactionally with each invoice/job, lease/token claims, exponential bounded retries, exhausted-event review/audit handling, restart polling, migration/local-schema backfill for pre-existing queued work, and admin-only inspection/retry. Invoice details and the admin UI expose safe dispatch state; idempotent replay respects backoff and does not resend a published event.
- CPU path for digital PDF text and scanned image/PDF OCR; structured rule-first extraction with source/page/bounding box, field validation and source-confidence metadata. Tesseract calls are limited by per-call and per-document time budgets; a timeout fails safely into the existing human-review path. An optional local token-classification adapter exists behind `LAYOUT_MODEL_PATH`; it is disabled by default, uses only a local fine-tuned checkpoint, preserves deterministic fallback evidence, and requires human validation. No checkpoint or accuracy claim is bundled.
- Three-way matching with `Decimal` arithmetic, configurable tolerance, line mapping, vendor/currency/PO-status checks, per-invoice and cumulative non-rejected invoice quantities, receipt checks, tax/subtotal/total arithmetic, manual exceptions for unallocated freight/discount, explainable discrepancies, and an estimated exposure value.
- Duplicate and anomaly screening, vendor-history indicators, vendor/employee and vendor/vendor relationship checks over tokenized identifiers, and Benford eligibility gating. Near-duplicate review candidates can use one-to-one SKU/description and numeric line agreement; an optional local-only sentence-transformer adapter can add cosine similarity only when an approved local checkpoint is supplied. No model weights are bundled, downloads are disabled, and no semantic detection performance is claimed. Evidence stores scores and row counts, not raw line text or bank/address tokens. Small/unsuitable samples do not receive a Benford conclusion.
- Review workflow with assignment and decisions, human corrections and reason capture, versioned extraction records, event audit verification, overview/reconciliation/fraud/performance analytics, and exports. A separate persisted proposal workflow snapshots invoice/vendor/match/PO/GRN state, requires an independent `approver` role, blocks self-approval and prior invoice contributors, audits all decisions, and offers only a local non-executable review packet with no bank details or outbound call. Admins can page through correction pairs explicitly approved after resolved human review as JSONL; reviewer notes/source files are omitted and export does not train or promote a model.
- `ml/evaluate_extraction.py` local-only labeled JSONL benchmark utility for per-field exact/normalized matches, numeric MAE, one-to-one line-item metrics, and descriptive confidence bins; it never trains, invents labels, or sends data externally.
- Invoice register with server-side search, workflow/processing status filters, and 50-row offset pagination. Invoice, review-case, and vendor invoice list payloads batch related summaries instead of issuing per-row lookups; synthetic query-count regression tests cover 25-row request fixtures.
- Dashboard charting is lazy-loaded as a separate frontend chunk; the latest production build has a 325.20 kB main JS chunk (91.87 kB gzip) and a 423.44 kB chart chunk (114.61 kB gzip), avoiding the prior Vite large-chunk warning. This is a bundle split, not a measured end-to-end latency improvement.
- Synthetic seed fixtures, Alembic initial revision, Dockerfiles/Compose definition, and backend/frontend tests.

## Mocked, not bundled, or incomplete

- **External providers:** the generic REST ERP adapter and signed RFC822 webhook are mock-tested interfaces only; no authorized ERP sandbox, mailbox, or email provider is connected. There is no invoice-status/approval/payment write, cloud OCR, bank validation, SSO/SCIM, or payment API. Local/manual procurement APIs remain available.
- **Layout model:** a local token-classification adapter is available only when explicitly configured, but no LayoutLM checkpoint or trained extraction model is distributed. The default remains rule-first; a model softmax score is uncalibrated, model candidates are forced to human review, and the local Isolation Forest/anomaly utilities are not a labeled fraud classifier.
- **Payments:** there are no payment rails, execution endpoints, scheduling jobs, or ERP payment writes; `auto_payment_enabled` is false. A proposal may be created from eligible invoice evidence and explicitly approved by a separate `approver`, but approval only changes the proposal record. The optional handoff downloads a local JSON review packet marked `not_payment_instruction: true`, `executable: false`, and `payment_executed: false`; no bank details are included and no external call occurs.
- **Production controls:** Compose is a local demo, uses development mode, optional schema auto-create and demo seed. No WORM audit storage, KMS, secrets manager, TLS termination, SSO, organization tenancy, production backup/restore drill, or independent security review is supplied.
- **Scanned OCR:** requires system Tesseract. The health endpoint reports availability; absence does not silently become an external OCR call.
- **Scale/coverage:** the invoice register is server-paginated at 50 rows per request and common related list payloads are batched, but review screens and other routes are still intended for demonstration-sized data. No load test has run; a narrow local Playwright suite passes, but it is not broad browser-workflow coverage. Docker Compose was not run in this verification.

## Domain semantics

- Matching outcomes are data/reconciliation states, not judgments about intent.
- `PARTIAL_MATCH` can reflect valid partial delivery. `MISSING_PO`/`MISSING_GRN` are exceptions, not fraud labels.
- The heuristic risk score/severity are prioritization indices, not calibrated probabilities.
- “Estimated exposure” is rule-based arithmetic from current invoice and receipt context, not a confirmed loss or a prevented payment.
- Synthetic seed records and their anomalies are demonstration scenarios, not sampled from production ledgers.
