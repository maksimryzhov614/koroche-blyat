# Behavior baselines

This directory stores immutable raw evidence captured by `python -m evals.run_control`.

- `--dry-run` is offline and writes no evidence.
- Live inference is forbidden unless `--confirm-live` is passed explicitly.
- A run directory must be empty before capture; existing evidence is never overwritten.
- `manifest.json`, `responses.jsonl`, and `raw/` come only from real subprocess runs.
- `manual-review.md` comes only after a human reads every flagged recorded answer and cites its response SHA-256.
- Never add synthetic responses, placeholder hashes, credentials, or unobserved failure claims.
