# Backup and recovery — procedure design; restore not yet verified

**Status:** No production database, object-storage bucket, backup schedule, RPO/RTO, or restore drill is configured. This document describes required controls and safe rehearsal steps; it is not evidence that a backup exists or can be restored.

## Data that must be protected

1. PostgreSQL business records and Alembic revision.
2. Private original invoices, page/image derivatives, and quarantined bytes, with object metadata/content hashes.
3. Outbox/job/receipt/idempotency and auth-session state required for coherent recovery.
4. Audit events and any separately replicated immutable audit sink.
5. Model artifacts/metadata and approved evaluation datasets, if later added.
6. Deployment secret recovery material held separately from database/document backups.

The database and document storage are separate durability domains. Do not assume a PostgreSQL backup contains invoice bytes or that a storage snapshot includes a coherent DB snapshot.

## Owner decisions required

Before configuring schedules, the system owner must approve:

- Recovery Point Objective (RPO) and Recovery Time Objective (RTO).
- Data region, retention, encryption-at-rest, key custody/rotation and deletion rules.
- Whether quarantined documents are backed up, and their separate access/retention policy.
- Who can create, decrypt, restore and validate backups; separation of duties.
- Backup frequency, retention count, immutable/offsite copy, alert destination and cost ceiling.

Do not fabricate these values or put encryption keys beside the backups they protect.

## Minimum backup design

- Use the hosted PostgreSQL provider's supported, encrypted backup/PITR facility and retain an independent export/snapshot according to the approved policy.
- Back up document/object storage with versioning or an equivalent recoverable copy; record object keys, byte sizes and SHA-256 checksums.
- Store backup metadata and checksums in a separate security boundary. Protect backup keys through an approved KMS/secret-management service.
- Monitor last-success time, backup age, capacity, errors, and deletion/retention jobs. Alert on missed windows.
- Preserve migration/release version metadata and the tested archive SHA outside the application database.

## Restore rehearsal (must be executed in an isolated environment)

1. Obtain change approval; create a separate restore project/database/bucket. Never overwrite production or the preserved workspace DB during a test.
2. Record restore start time, backup timestamp, source Alembic revision, target version, operator and incident/change ID.
3. Restore PostgreSQL using the provider-supported process and restore the corresponding document/quarantine snapshot into isolated storage.
4. Apply only reviewed forward migrations to the target release; do not use `AUTO_CREATE_SCHEMA` or demo seeding.
5. Verify Alembic head, foreign keys, row counts, orphan/missing document references, object checksums, audit-chain verification and selected synthetic/authorized invoice opens.
6. Verify outbox/processing-job state is coherent. Do not automatically dispatch duplicate work or payments after restore; pause workers until an operator checks the recovery plan.
7. Measure elapsed recovery time and data loss window. Compare against owner-approved RPO/RTO.
8. Retain sanitized logs, SQL/provider status, checksums and signed result. Delete the isolated test data according to retention policy.

## Recovery boundaries

- Filesystem and database commits are not atomic. Existing upload receipts/idempotency/recovery paths help repair selected interrupted uploads, but hard process loss, region loss and full backup restore have not been proven. The U03 status endpoint is a read-only diagnostic for a single actor-scoped upload; it is not a backup, repair mechanism, or guarantee that file storage can be restored.
- Payment execution is not implemented. Recovery must never create or transmit a payment instruction.
- A Render persistent disk is per service and is not a shared API/worker volume; it is not an acceptable shared document-storage design for the present multi-service topology.

No backup-restore test has run. The current requirement remains blocked until a real isolated restore and consistency checks pass.
