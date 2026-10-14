# matrix-sms-gateway

Matrix application service that bridges a private Matrix group room to SMS/MMS-only members via Telnyx 10DLC.
Each phone number is a real ghost user in Matrix.

Status: design. See [docs/BRIEF.md](docs/BRIEF.md), [docs/DECISIONS.md](docs/DECISIONS.md),
[docs/VERIFIED.md](docs/VERIFIED.md).

Secrets live in `.env` only (gitignored).
