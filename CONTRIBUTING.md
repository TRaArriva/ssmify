# Contributing

Thanks for your interest in ssmify.

## Before you start

ssmify modifies IAM roles and instance profiles on live AWS accounts. Any contribution that touches IAM logic, network checks, or credential handling requires extra scrutiny — please open an issue to discuss the change before sending a PR.

## How to contribute

1. **Open an issue first** for anything beyond a typo fix — describe the problem or feature so we can align before you invest time writing code.
2. Fork the repo and create a branch from `main`.
3. Make your changes. Run `ruff check ssmify.py` before committing.
4. Open a pull request. Fill in the PR template.

## What we look for in PRs

- No hardcoded AWS account IDs, instance IDs, or credentials — not even in tests or comments.
- IAM changes must be least-privilege: only request what is strictly needed.
- New flags or behaviors must work correctly with `--assume-yes` and `--dry-run`.
- Keep the dependency list minimal. boto3 is the only runtime dependency by design.

## Reporting a vulnerability

See [SECURITY.md](SECURITY.md).
