## What does this PR do?


## Checklist

- [ ] Tested with `--dry-run` — no unintended AWS changes
- [ ] Tested with `--assume-yes` — no stdin blocking
- [ ] No hardcoded AWS account IDs, instance IDs, or credentials
- [ ] IAM changes are least-privilege (only what is strictly needed)
- [ ] `ruff check ssmify.py` passes
- [ ] README updated if behavior or flags changed
