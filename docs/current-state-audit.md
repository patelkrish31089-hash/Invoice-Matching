# InvoiceGuard AI — current-state audit

**Audit date:** 2026-10-17 (Asia/Calcutta)  
**Scope:** Existing `/home/user/invoice-fraud-platform` tree, the seven-page supplied specification PDF, current source/tests/migrations/CI/Compose/deployment notes, and the preserved local verification evidence. This is a source/evidence review, not a penetration test or production readiness assessment.

**Selected target scope:** A single-organization private demo. The application has no organization/tenant isolation; restrict access to one owner-approved organization and do not claim cross-organization protection. The owner will perform the private GitHub/Render browser steps and share non-secret results. The owner has indicated authorized, de-identified benchmark data can be provided; no dataset has been uploaded to this workspace.

## Executive result

The project is a functioning **local synthetic-data AP demo** with real code for authenticated upload, bounded PDF/image/DOCX handling, OCR/rule extraction, deterministic matching, heuristic fraud signals, review/correction, audit, an outbox, and local browser workflows. The selected destination is a **single-organization private demo**, not a multi-tenant product. It is not a fully verified or deployed production service. Several material targets require owner-authorized data, provider access, private deployment infrastructure, or a compatible storage design; they cannot be established from code or synthetic tests alone.

The detailed acceptance register is `docs/traceability-matrix.md`. Current matrices use only `PASS`, `PARTIAL`, `FAIL`, `BLOCKED`, and `NOT RUN`; a status applies only to the named test and environment. `BLOCKED` and `NOT RUN` are not passes.

## Specification reviewed

The supplied PDF at `/home/user/uploads/invoice-matching-fraud-detection-data-science-2026-10-12.pdf` is seven pages. It describes multi-channel document intake, OCR with word boxes, LayoutLM or equivalent spatial NLP, line-item/table parsing, deterministic invoice–PO–GRN matching, duplicate detection, Benford screening, Isolation Forest, vendor/employee relationship graphs, human review, active learning, ERP integration, and claimed business benefits.

The PDF's `>98%` extraction objective and fraud-performance claims need representative independently labeled holdouts. The PDF also proposes automatic approval and payment. That part conflicts with the user's standing safety constraint and remains deliberately disabled; human review must not be interpreted as a payment instruction. Clause-level mapping and scope conflict are in `docs/traceability-matrix.md`.

## Inventory and findings

| Area | Current implementation/evidence | Audit assessment |
|---|---|---|
| Frontend/API | React + TypeScript/Vite, FastAPI, typed API models, role-checked endpoints, server-side pagination, local browser workflows | Functional for the tested local synthetic paths; broader accessibility, authorization, scale, and hosted coverage are incomplete. The application is a shared single-organization workspace, not multi-tenant isolated. |
| Persistence | SQLAlchemy, additive Alembic chain through `a71f3c2e9b40`, SQLite local tests, PostgreSQL Compose configuration | Disposable SQLite migrations/preservation tests pass. Live PostgreSQL migrations/concurrency and an upgrade against a separately restored real-data copy have not run. The existing workspace DB was not changed. |
| Upload/security | Signature/size checks, private local storage, scanner adapter, quarantine metadata, durable receipts, idempotent replay, recovery, outbox | Important fail-closed and replay behavior is tested synthetically. Storage and SQL are separate durability domains; process-kill recovery is incomplete; actual ClamAV is unverified. |
| OCR/extraction | PyMuPDF, Tesseract/OpenCV path, rules, confidence/evidence metadata, optional local token-classifier adapter | Host Tesseract integration passed on synthetic fixtures. No approved LayoutLM checkpoint or representative corpus; `>98%` is unverified/blocked. |
| Matching/fraud | Decimal-based PO/GRN rules, cumulative quantity checks, duplicate/Benford/Isolation Forest/relationship heuristics | Selected synthetic regressions pass. No real PostgreSQL concurrency, adjudicated fraud labels, calibrated threshold, or precision/recall evidence. Scores are not fraud probabilities. |
| Human review/audit and payment proposals | Assignment, reasoned correction/disposition, hash-linked audit, vendor-only upload scope, persisted non-executable proposals, independent `approver` role, separation-of-duties checks, and local non-payment review packet | Local synthetic tests cover proposal eligibility, independent approval/rejection, cancellation, stale snapshots, audit, and local packet export. Hash-linked DB audit is not WORM; cross-organization isolation is not implemented (and is out of scope for the selected single-organization demo), and independent retention is absent. No payment rail or execution path exists. |
| ERP/email | Generic read-only REST mock adapter; generic signed RFC822 webhook | Mocks are not provider verification. No authorized provider, sandbox credential, real webhook delivery, or real integration test exists. |
| CI/security | GitHub workflow includes backend/frontend/browser/security/container/release-gate jobs and audits | YAML/shell/report contracts were checked locally. Hosted Actions, Trivy source/image scans, Docker builds, and branch rules have not been observed. |
| Deployment/storage | Dockerfiles/Compose describe API, UI, PostgreSQL, Redis/Celery, ClamAV, worker and dispatcher. U04 now makes the API listener honor `PORT` and Nginx use configurable `PORT`/`API_UPSTREAM`; static template tests pass. | No deployed URL or target run exists. **Render is not a drop-in target for the current local shared-volume design:** its persistent disk is attached to one service instance and cannot be shared between API and worker. A selected object-storage/shared-storage design and code adapter are needed before safe multi-service Render deployment. Docker image build/runtime, private routing and hosted health remain unverified. |
| Operations | Health/readiness endpoints and recovery notes | No backup restore, RPO/RTO, monitoring/alert, load, incident drill, or independent security review has been executed. |

## Local verification evidence

The U03/U04/U01-inclusive candidate was tested locally on 2026-10-17. Current test reports, JUnit and logs are retained in `docs/verification/evidence-2026-10-17/`; earlier strict Python dependency-audit reports remain in `docs/verification/evidence-2026-10-16/`. The human-readable summary is `docs/verification/README.md`.

- Clean backend setup/tests with `REQUIRE_TESSERACT=1`: **147 passed, 0 skipped, 3 deprecation warnings**; `pip check`, Celery import, and disposable SQLite migration through `a71f3c2e9b40` passed. Tesseract 5.5.0 with `eng`/`osd` data was present; OCR integration ran on synthetic fixtures only. JUnit XML and the required-Tesseract preflight log are retained in `docs/verification/evidence-2026-10-17/`. No accuracy claim follows from fixture tests.
- Frontend: **43 passed**; TypeScript/Vite production build passed. `npm ci` and current `npm audit --audit-level=high --json` reported 0 known vulnerabilities.
- Chromium E2E: **8 expected, 0 skipped, 0 unexpected, 0 flaky**. It covers U03 recovery, vendor-only access, review/source-box paths, and a three-actor U01 scenario: reviewer approval, proposal authoring, separate approver, and distinct AP-clerk download of a local packet marked not a payment instruction and non-executable. All data is synthetic, API/Vite are local, and test state is temporary. The run used `--retries=0` to avoid replaying the workflow against mutated disposable demo records.
- U03: local FastAPI/SQLite tests verify actual durable receipt/invoice/job/dispatch state, actor/source/vendor scope, indistinguishable unknown/out-of-scope keys, and no DB/storage mutation or dispatch.
- U01: targeted backend/migration tests pass; approvers can inspect invoice/detail but cannot change extraction or decide review; proposal creation/approval/cancel/handoff are audited and no payment or ERP call exists. Fresh Chromium validates the non-executable packet flags and observes no external network request.
- U04: static Docker/Nginx template tests pass; Docker images, Render private routing and shared storage were not run.
- Prior `pip-audit` reports for unchanged manifests show no known vulnerabilities in selected resolver graphs; current npm audit is clean. Trivy and hosted CI remain unrun. Local evidence is not a hosted CI or production result.
- The clean candidate archive was rebuilt from the latest source/docs, validated for ZIP integrity, one-root layout, exact member set, exclusions and source-byte identity, then independently extracted and passed `scripts/verify_clean.sh`. The baseline ZIP was preserved. The final archive SHA-256 and exact member list are stored outside the ZIP in `docs/verification/evidence-2026-10-17/`.

## Highest-priority gaps

1. **Data/model evidence:** no authorized representative invoice labels, approved fine-tuned spatial checkpoint, or adjudicated fraud/duplicate outcomes. Accuracy, fraud precision/recall, calibration, ROI, and avoided-loss remain unmeasured.
2. **Deployment architecture:** owner-run private GitHub/Render browser steps and non-secret results are pending; no deployment URL exists. U04 static `PORT` and Nginx upstream templates pass tests, but Docker image/runtime, private access, routing and ClamAV are unverified. Render disks are per-service while API/worker currently share a filesystem volume; choose and implement approved private shared/object storage before splitting those services on Render.
3. **Real infrastructure:** Docker is unavailable in this environment; no live PostgreSQL/Redis/Celery/ClamAV, PostgreSQL concurrency/migration, EICAR, service restart, or hosted workflow result exists.
4. **Security/operations:** the selected single-organization scope is not multi-tenant isolated; enforce private access and do not onboard other organizations. Independent security review, encryption/key-management policy, distributed rate limits, WORM audit, backup restore, monitoring, and tested incident response remain absent.
5. **Providers:** no ERP/email provider selection, sandbox credentials, or authentic provider delivery. Generic adapters and mocks do not establish compatibility.

## Prioritized implementation plan

### P0 — Owner decisions and prerequisites (no destructive migration or external transmission)

- **Single-organization private demo confirmed.** Do not add tenant columns, backfill the preserved DB, onboard another organization, or claim multi-tenant isolation without a separately approved scope and migration/security design. Enforce the private access boundary before any hosted document use.
- Select Render vs another host and a **shared/private object-storage provider** for invoice/quarantine bytes; authorize security, retention, encryption, and region requirements. Do not put real invoices in chat or send them to a provider without approval.
- The owner has indicated an authorized, de-identified benchmark can be provided; none is in the workspace yet. Before evaluation, agree field/line label definitions, independent adjudication, vendor/time holdout, and data handling. Separately obtain reviewed fraud/duplicate outcomes before reporting fraud metrics.
- Supply a licensed compatible model checkpoint/label schema only if real spatial-model inference is required.
- Select ERP/email providers and authorized sandbox arrangements; do not paste credentials in chat.

### P1 — Safe local engineering work — implementation, candidate-wide local verification, and clean release archive validation passed

- U03: added the authenticated actor/source/vendor-scoped read-only upload-status lookup, same-tab reference, and manual UI check. Tests verify non-disclosure, actual durable state, no mutation/dispatch, status-before-retry and reload recovery. `PASS` is limited to the named synthetic API/SQLite/UI cases.
- U04: API Docker start honors `PORT`; frontend Nginx templates `PORT` and `API_UPSTREAM` with Compose-safe defaults. Static template tests pass; container runtime/Render remain unverified.
- U01: added an additive migration and persisted non-executable payment-proposal model/API/UI; a dedicated `approver` role with read-only invoice/detail access; required proposal reasons; stale invoice/vendor/match/PO/GRN snapshot checks; separation from submitters, correctors, reviewers and proposal authors; audited approve/reject/cancel/handoff; and a local-only JSON packet explicitly marked not a payment instruction. The approver cannot change invoice extraction or decide review cases. Targeted and full synthetic backend tests, frontend tests/build, and fresh 8/8 Chromium E2E pass. No external payment/ERP call, payment execution, or automatic approval exists.
- Reconciled the requested audit, traceability, security, deployment, testing, backup/recovery, incident-response and release-note docs; the canonical project traceability matrix remains authoritative.

### P2 — Run real infrastructure and CI gates

- In an owner-controlled Docker-capable repository, run all required workflow jobs; inspect raw artifacts and require `release-gate` in branch protection.
- Execute disposable PostgreSQL migrations/concurrency tests, Redis/Celery/outbox restart/replay, real ClamAV clean/EICAR/unavailable cases, and service restart/persistence checks.
- Resolve findings from Trivy source-secret and image scans; run only against the exact candidate images.

### P3 — Evaluation and governed model work

- With authorized labels, evaluate OCR fields/lines, duplicate candidates, fraud signals, operating thresholds, uncertainty and segment performance. Do not mark the `>98%` or fraud-metric requirements `PASS` until the defined held-out acceptance criteria pass.
- Run a real, licensed checkpoint against the same held-out corpus and compare with the deterministic baseline before enabling model inference.

### P4 — Staging and deployment

- Implement the selected shared object-storage adapter, test its permissions/lifecycle/failure recovery, and resolve Render private-service/proxy/ClamAV wiring.
- Deployment status: **BLOCKED**. No owner-run GitHub/Render result, private access proof, Docker staging run, compatible shared/object-storage adapter, or deployed URL has been reported. After those gates are resolved, rehearse migrations from a verified backup and test upload-to-review/restart and same-organization role boundaries before recording any URL/release. Cross-organization isolation is not provided. Do not claim `DEPLOYED`.

No destructive DB or storage change was made during this audit. Payment execution and autonomous approval remain disabled.
