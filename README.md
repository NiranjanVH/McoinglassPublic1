# BTC signal trader

The manually triggered GitHub Actions workflow is configured for **live
trading**. Each run can place or cancel leveraged futures orders based on the
latest Telegram signal. It requires strict SSH host-key verification and will
refuse to run live if verification is disabled.

Before proceeding, it compares the directions of the latest two valid BTC
signals in the Telegram chat. It continues with the newest signal only when
both are LONG or both are SHORT; otherwise, it skips the run.

Before running it:

1. Add the OCI VM's verified Ed25519 host-key entry as the `OCI_SSH_KNOWN_HOSTS`
   Actions secret. Its host and port must match `OCI_VM_HOST` and `OCI_VM_PORT`.
   Verify the fingerprint through a trusted source; do not use an unverified
   key.
2. Confirm all Telegram, CoinDCX, and OCI Actions secrets and trading settings.
3. Trigger **Run BTC signal trader** only when ready for a live trade.

The script uses 27x leverage by default and does not place stop-loss orders.
For local development, copy `.env.example` to `.env`; dry-run and strict
host-key verification are the local defaults.
