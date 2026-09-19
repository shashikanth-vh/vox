# PRISM live backup — near-zero data loss

The nightly `pgbackup`/`filebackup` sidecars bound data loss to a day. This layer
bounds it to **about a minute**, for everything stateful:

| What                         | Mechanism                              | RPO      |
|------------------------------|----------------------------------------|----------|
| Postgres (all databases)     | continuous WAL → S3 + daily base       | ≤ ~90 s  |
| MinIO objects (docs, audio)  | volume mirror → S3, every 60 s         | ≤ ~2 min |
| vocx state (Google tokens)   | tar snapshot → S3, every 6 h           | ≤ 6 h    |
| pulse schedules / dex        | tar snapshot → S3, every 6 h           | ≤ 6 h    |
| Nightly dumps (kept!)        | unchanged — corruption-independent net | 24 h     |

The database restore is **point-in-time**: not "last night" but *"to 14:32,
right before the mistake"* — which also protects against accidental deletes,
something replication alone never can.

## One-time setup

1. **S3 bucket** (same region as the VM): create e.g. `prism-livebackup-<org>`.
   - Enable **versioning** (protects the MinIO mirror against deletions and a
     compromised box overwriting history).
   - Lifecycle rules: expire noncurrent versions after 30 d; expire
     `<prefix>/wal/` objects after 35 d (must outlive the oldest kept base).
2. **IAM user** `prism-livebackup` with access keys and this policy (only this
   bucket, no delete of noncurrent versions — ransomware on the box cannot
   destroy history):
   `s3:PutObject`, `s3:GetObject`, `s3:ListBucket`, `s3:DeleteObject` on
   `arn:aws:s3:::<bucket>` and `arn:aws:s3:::<bucket>/*`.
3. **`deploy/compose/.env`** on the box:

   ```
   WAL_ARCHIVE_MODE=on
   LIVEBACKUP_S3_BUCKET=prism-livebackup-<org>
   LIVEBACKUP_AWS_ACCESS_KEY_ID=...
   LIVEBACKUP_AWS_SECRET_ACCESS_KEY=...
   LIVEBACKUP_AWS_REGION=ap-south-1
   ```

4. Recreate: `docker compose -p compose up -d postgres livebackup-wal livebackup-objects`
   (or any full deploy — the sidecars ride the existing `backup` profile).

Everything defaults **off**: without `LIVEBACKUP_S3_BUCKET` the sidecars idle
and log why; without `WAL_ARCHIVE_MODE=on` postgres behaves byte-identically to
before.

## Verifying it runs

```
docker logs compose-livebackup-wal-1 --tail 5       # "base ... uploaded", no FAILED
docker logs compose-livebackup-objects-1 --tail 5
aws s3 ls s3://<bucket>/prism/wal/ | tail -3        # fresh segments, minutes old
```

Failure markers (surfaced by prism-offsite status too): `WALSHIP_FAILED` in the
walarchive volume, `OBJSHIP_FAILED` in the pgbackups volume. A present marker
means the pipeline is NOT protecting you — treat like a failed nightly.

## Restore

* **Database to any second**: `sudo deploy/backup/restore-pitr.sh "2026-09-19 10:30:00+00"`
  — announces every step, takes a safety snapshot of the current data, asks for
  the literal word RESTORE before touching anything.
* **Objects**: normally NOT rewound (documents are write-once; a DB rewind
  rarely wants files deleted). When needed:
  `aws s3 sync s3://<bucket>/prism/minio-live/ <dir>` into the miniodata volume
  (stack stopped), or recover an individual deleted object from S3 versioning.
* **Aux volumes**: pull the wanted `volumes/<name>/<name>-<ts>.tar.gz` and untar
  into the volume (stack stopped).

## Drill (quarterly, 30 minutes, on staging)

1. Note a marker: create a lead named `DRILL-<date>` on staging, note the time.
2. Delete it five minutes later.
3. `restore-pitr.sh` to the minute between create and delete.
4. Confirm the lead exists again; confirm the newest recordings play (MinIO).
5. Record the drill date and duration here in a PR.

A backup that has never been restored is a hypothesis, not a backup.
