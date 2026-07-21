# Security Policy

## Scope

ssmify is a tool that modifies AWS IAM roles and EC2 instance profiles. A vulnerability in this tool could result in unintended privilege escalation, unauthorized access to AWS resources, or unexpected costs. Security reports are taken seriously and addressed promptly.

## Supported Versions

Only the latest release on the `main` branch is actively maintained.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please report them privately via [GitHub's security advisory system](https://github.com/TRaArriva/ssmify/security/advisories/new). Include:

- A description of the vulnerability and its potential impact
- Steps to reproduce (sanitized — no real AWS account IDs or instance IDs)
- Any suggested fix, if you have one

You can expect an acknowledgement within 5 business days and a resolution or status update within 30 days.

## Responsible Use

ssmify is intended for use on AWS infrastructure you own or are explicitly authorized to manage. Using it on accounts or instances without authorization may violate AWS's terms of service and applicable law. The maintainers are not responsible for misuse.
