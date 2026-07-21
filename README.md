# ssmify

![ssmify](logo.png)

Get any EC2 instance on SSM Session Manager — no SSH, no key pairs, no open ports.

```bash
python3 ssmify.py -i i-0123456789abcdef0 -p myprofile -r eu-west-1
```

ssmify diagnoses the three reasons an instance isn't SSM-reachable, fixes what it can (IAM, network), then polls until the agent registers.

## Install

```bash
pip install boto3
```

For the optional interactive shell at the end, you'll also need the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) on your local machine.

## Usage

```bash
# single instance
python3 ssmify.py -i i-abc123 -p prod -r eu-west-1

# multiple instances, unattended
python3 ssmify.py -i i-aaa i-bbb -p prod -r eu-west-1 --assume-yes

# preview without touching AWS
python3 ssmify.py -i i-abc123 --dry-run
```

| Flag | Description |
|------|-------------|
| `-i`, `--instances` | Instance IDs (space-separated) |
| `-p`, `--profile` | AWS profile |
| `-r`, `--region` | AWS region |
| `-y`, `--assume-yes` | Skip confirmation prompts |
| `--dry-run` | Show what would change, touch nothing |
| `--role-name NAME` | IAM role to create (default: `ec2-ssm-core`) |
| `--wait-timeout SECONDS` | Poll timeout in seconds (default: `600`) |
| `--no-network-check` | Skip the VPC reachability check |

## How it works

Three things must all be true for an instance to appear in SSM. Miss any one and the instance silently stays Offline.

| | What | What ssmify does |
|---|------|-----------------|
| 1 | **Agent** installed and running | Detects OS family; warns if agent likely isn't preinstalled |
| 2 | **IAM** role with `AmazonSSMManagedInstanceCore` | Attaches to the existing role, or creates `ec2-ssm-core` |
| 3 | **Network** path to SSM endpoints (outbound 443) | Checks for IGW, NAT gateway, or VPC interface endpoints |

After fixing IAM, the agent picks up new credentials from IMDS without a reboot. ssmify polls until it registers — on instances that ran a long time without a role, this can take up to 10 minutes as the agent works through its back-off timer.

## Troubleshooting

**Instance never comes Online after the timeout.**
IAM and network are already handled, so the cause is the agent — either not installed, or stuck in a back-off. It will usually self-register once the timer clears. To force it immediately:

```bash
# restart the agent (if you have another way in)
sudo systemctl restart amazon-ssm-agent   # Linux
Restart-Service AmazonSSMAgent            # Windows

# or reboot the instance
aws ec2 reboot-instances --instance-ids <id>

# or just wait longer
python3 ssmify.py -i <id> --wait-timeout 1800
```

**`session-manager-plugin` not found.**
The interactive shell step needs the plugin on your machine, not the instance. ssmify skips it gracefully if missing. [Install it here.](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)

## License

Apache 2.0
