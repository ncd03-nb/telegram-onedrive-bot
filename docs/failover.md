# Docker ↔ Render automatic failover

Cloudflare `telegram-bot-failover` holds the permanent Telegram webhook, pending commands, controller lease and a compressed application database snapshot. Each bot has an independent Durable Object with SQLite storage. It stores no Telegram, Google, Microsoft or GitHub credential. Host authentication and webhook authentication use different HMAC keys derived from the bot token.

Docker and Render use `HUB_URL`, `HUB_BOT_ID=onedrive|payload`, and `HUB_ROLE=docker|render`. OneDrive also requires `BOT_MODE=hub` and `TRANSFER_BACKEND=actions`. In this mode neither host changes the webhook or calls Telegram `getUpdates`.

Active hosts and Docker check ownership and commands every 5 seconds; passive Render hosts check every 15 seconds. Ownership expires after 60 seconds without renewal; a graceful Docker shutdown releases it immediately. Render takes over only after a synchronized state exists. Docker returning has priority once Render has no command, job or cleanup in progress. The old lease epoch cannot write a checkpoint or acknowledge commands. A host that loses connectivity stops its controller tasks.

The database snapshot is committed **before** dispatching Actions, including the unique task ID and dispatch intent. A restarted controller reconciles the existing workflow by unique run name, and never repeats an ambiguous dispatch. A crash between the checkpoint and the POST can leave a task needing manual reconciliation instead of creating a duplicate upload. Telegram notifications themselves do not have an exactly-once API.

Actions notify the hub when result metadata is written. This wakes Render if Docker is absent. Actual queued commands and OneDrive deletion deadlines also wake it. The hub does not ping Render merely to defeat Free idle sleep. Free Render cold starts can add approximately a minute to the 60-second ownership timeout; free quotas and availability still apply. The shared snapshot preserves OneDrive's original 12-hour expiration and file identity guard. Payload results have no automatic deletion policy.

## Deployment and recovery

1. Run `python scripts/setup_failover.py --payload <payload repo> --hub <Worker origin> --payload-render <Render origin> --configure-actions`. It creates private import files under ignored `config/`. Never commit or print them.
2. Deploy `cloudflare-failover` with `wrangler deploy --secrets-file ../config/failover-secrets.json`. Use the Workers Free plan and SQLite Durable Objects.
3. Import each private `config/render-failover.env` into its corresponding Render Free Docker service. Verify `/health` reports role `render` and no local Telegram poller.
4. Back up live SQLite via the running container, stop the old pollers, add the hub configuration to Docker's private `.env`, and start Docker first to seed existing state.
5. Set each Telegram webhook to `<Worker origin>/telegram/<bot>` with its `webhook_key`, without dropping pending updates. Check `getWebhookInfo` and the authenticated hub `/api/<bot>/status`.
6. Test stopping Docker, receiving a command on Render, then restarting Docker and confirming safe return. Compare task IDs and workflow IDs before/after. Use `/health` and authenticated hub status to diagnose ownership; never inspect a Linux live SQLite database with host Windows SQLite.

Render state on disk is disposable **only after** a verified hub snapshot exists. Do not reset Durable Object storage during a recovery. Rotate the derived hub keys and webhook secret together if the Telegram bot token changes. Restore from a backup only when all controllers are stopped and the authoritative shared state has been assessed.

The snapshot is limited to 600 KB compressed and 16 MB expanded. Exceeding it stops ownership renewal instead of losing state; prune expired scans/completed update payloads or raise the reviewed limit before continuing.
