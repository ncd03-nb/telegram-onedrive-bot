# Cloudflare controllers and Actions workers

Both Telegram controllers run on the `telegram-bot-failover` Cloudflare Worker, with one SQLite Durable Object per bot. Docker and Render controller leases are rejected after each bot is activated. Telegram's webhook URL stays unchanged.

Cloudflare handles commands, ownership checks, job state, Actions dispatch and reconciliation, output links and OneDrive's guarded 12-hour deletion. Payload metadata scans use `scan.yml`; extraction uses `extract.yml`. Copying to OneDrive uses `copy.yml`. File bytes and image decompression run on GitHub-hosted runners.

Actions update the existing Telegram progress message approximately every two seconds when progress changes, with a ten-second heartbeat during unchanged progress. Telegram's `retry_after` is respected. Completed multi-partition jobs produce one ZIP, one result message, ROM version and member names. Reply/topic identity is preserved.

The repositories may be public. Job inputs and result artifacts use AES-256-GCM with a fresh 12-byte nonce and task ID as authenticated associated data. The key is derived from the bot token with a separate HMAC purpose. Public logs and artifacts expose the random job ID and status; source URLs, Telegram identities and download capabilities remain encrypted. Bot/OAuth/GitHub credentials remain in Cloudflare/GitHub secrets and ignored local config files.

## Migration

1. Back up each live database through its Docker container. Prepare Worker credentials using `python scripts/prepare_cloudflare.py --payload <payload repo>`; never commit the resulting private file.
2. Deploy the Worker with `wrangler deploy --secrets-file ../config/cloudflare-controller-secrets.json`. Existing controllers continue until staged.
3. Call `python scripts/failover_control.py prepare --bot <onedrive|payload>` to block replacement legacy leases. Stop each existing Docker controller, which saves its final snapshot and releases its lease.
4. Call `python scripts/failover_control.py activate --bot <bot> [--repo <payload repo>]`. Import jobs, scans and output links without changing OneDrive expiry or original deletion eTag. Compare authenticated `audit` with the local pre-migration backup.
5. Set the OneDrive repo variable `DOWNLOAD_BASE_URL` to the Worker origin. New links use `/0:/<capability>/<filename>`. `/link` refreshes the direct link and returns the Cloudflare Index Link.
6. Stop the old Render services after real command and Actions tests pass. Keep their repositories/config backups for recovery; do not automatically reset Durable Object storage or restart obsolete pollers.

The controller commits dispatch intent before calling GitHub. A network-ambiguous dispatch is reconciled by its unique run name instead of being repeated. The final workflow step sends encrypted result metadata to Cloudflare after artifact storage. Native alarms also check outstanding workflows, so build failures and missing callbacks can be recovered. Notification retries are stored separately from successful uploads.

The OneDrive Index endpoint returns a redirect to the current preauthenticated download URL; file bytes are served by OneDrive. File ID, size, content tag, signature and expiry are verified. Deletion uses the original eTag with `If-Match`, protecting files that were moved, replaced or edited. Payload output files keep the previous no-automatic-deletion policy.

Workers and Durable Objects remain subject to their free quotas, and GitHub Actions remains subject to runner availability and service policy. Controller migration does not increase CDN or Drive upload bandwidth.
