#!/usr/bin/env python3
"""ssmify - get an EC2 instance ready for AWS Systems Manager Session Manager.

Diagnoses and fixes the three things SSM needs (IAM, network path, agent),
then optionally opens a Session Manager shell - no SSH, no open ports.
See --help for the full guide.
"""

__version__ = "1.0.0"

import argparse
import shutil
import subprocess
import sys
import textwrap
import time

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound

SSM_MANAGED_POLICY_ARN = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
DEFAULT_ROLE_NAME = "ec2-ssm-core"

DOC_AGENT = "https://docs.aws.amazon.com/systems-manager/latest/userguide/ssm-agent.html"
DOC_AGENT_INSTALL = (
    "https://docs.aws.amazon.com/systems-manager/latest/userguide/"
    "sysman-manual-agent-install.html"
)
DOC_PLUGIN = (
    "https://docs.aws.amazon.com/systems-manager/latest/userguide/"
    "session-manager-working-with-install-plugin.html"
)

OS_PREINSTALLED = ["amazon linux", "amzn", "ubuntu", "windows", "macos", "sles", "suse"]
OS_NEEDS_INSTALL = ["red hat", "rhel", "centos", "debian", "rocky", "alma", "fedora"]

POLL_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 10


def info(msg):
    print(f"  {msg}")


def ok(msg):
    print(f"  \033[32m✓\033[0m {msg}")


def warn(msg):
    print(f"  \033[33m! {msg}\033[0m")


def fail(msg):
    print(f"  \033[31m✗ {msg}\033[0m")


def header(msg):
    print(f"\n\033[1m{msg}\033[0m")


def confirm(prompt, args):
    """Yes/no gate honouring --assume-yes and --dry-run."""
    if args.dry_run:
        info(f"[dry-run] would ask: {prompt}")
        return False
    if args.assume_yes:
        info(f"{prompt} -> yes (--assume-yes)")
        return True
    answer = input(f"  {prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def choose(prompt, options, args):
    """Numbered menu. Returns the chosen key, or None if skipped in dry-run."""
    if args.dry_run:
        info(f"[dry-run] would ask: {prompt}")
        return None
    if args.assume_yes:
        key, label = options[0]
        info(f"{prompt} -> {label} (--assume-yes)")
        return key
    print(f"  {prompt}")
    for i, (_, label) in enumerate(options, 1):
        print(f"    {i}) {label}")
    while True:
        raw = input("  choose a number: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print("  invalid choice")


def describe_instance(ec2, instance_id):
    resp = ec2.describe_instances(InstanceIds=[instance_id])
    reservations = resp.get("Reservations", [])
    if not reservations or not reservations[0].get("Instances"):
        raise ClientError(
            {"Error": {"Code": "NotFound", "Message": "instance not found"}},
            "DescribeInstances",
        )
    return reservations[0]["Instances"][0]


def describe_image(ec2, image_id):
    try:
        images = ec2.describe_images(ImageIds=[image_id]).get("Images", [])
        return images[0] if images else None
    except ClientError:
        return None


def check_os(ec2, instance, result):
    image = describe_image(ec2, instance.get("ImageId", ""))
    text = ""
    if image:
        text = f"{image.get('Name', '')} {image.get('Description', '')}".lower()
    if instance.get("Platform") == "windows":
        text += " windows"

    if any(k in text for k in OS_PREINSTALLED):
        ok("OS family ships the SSM agent preinstalled on AWS AMIs")
        result["os"] = "preinstalled"
    elif any(k in text for k in OS_NEEDS_INSTALL):
        warn("This OS usually does NOT ship the SSM agent preinstalled.")
        info(f"If the instance never comes Online, install it: {DOC_AGENT_INSTALL}")
        result["os"] = "needs-install"
    else:
        warn("Could not confirm the OS from the AMI metadata.")
        info(f"If it never comes Online, check the agent docs: {DOC_AGENT}")
        result["os"] = "unknown"


def is_managed(ssm, instance_id):
    resp = ssm.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
    )
    for entry in resp.get("InstanceInformationList", []):
        if entry.get("InstanceId") == instance_id:
            return entry.get("PingStatus") == "Online"
    return False


def role_name_from_profile_arn(iam, profile_arn):
    profile_name = profile_arn.split("/")[-1]
    profile = iam.get_instance_profile(InstanceProfileName=profile_name)
    roles = profile["InstanceProfile"].get("Roles", [])
    return roles[0]["RoleName"] if roles else None


def role_has_ssm_policy(iam, role_name):
    paginator = iam.get_paginator("list_attached_role_policies")
    for page in paginator.paginate(RoleName=role_name):
        for pol in page.get("AttachedPolicies", []):
            if pol.get("PolicyArn") == SSM_MANAGED_POLICY_ARN:
                return True
    return False


def attach_ssm_policy(iam, role_name):
    iam.attach_role_policy(RoleName=role_name, PolicyArn=SSM_MANAGED_POLICY_ARN)
    ok(f"attached AmazonSSMManagedInstanceCore to role '{role_name}'")


def ensure_role_and_profile(iam, role_name):
    """Create (idempotently) a role + instance profile with the SSM policy."""
    assume_policy = (
        '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
        '"Principal":{"Service":"ec2.amazonaws.com"},'
        '"Action":"sts:AssumeRole"}]}'
    )
    try:
        iam.get_role(RoleName=role_name)
        info(f"role '{role_name}' already exists, reusing it")
    except ClientError:
        iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=assume_policy,
            Description="Allows EC2 instances to be managed by AWS Systems Manager",
        )
        ok(f"created role '{role_name}'")

    if not role_has_ssm_policy(iam, role_name):
        attach_ssm_policy(iam, role_name)

    try:
        iam.get_instance_profile(InstanceProfileName=role_name)
        info(f"instance profile '{role_name}' already exists, reusing it")
    except ClientError:
        iam.create_instance_profile(InstanceProfileName=role_name)
        ok(f"created instance profile '{role_name}'")

    profile = iam.get_instance_profile(InstanceProfileName=role_name)
    attached_roles = [r["RoleName"] for r in profile["InstanceProfile"].get("Roles", [])]
    if role_name not in attached_roles:
        iam.add_role_to_instance_profile(
            InstanceProfileName=role_name, RoleName=role_name
        )
        ok(f"added role '{role_name}' to instance profile")
    return role_name


def _retry_on_invalid_profile(action, what):
    """Retry on InvalidParameterValue while a freshly created instance profile propagates through IAM."""
    deadline = time.time() + 60
    while True:
        try:
            return action()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "InvalidParameterValue" and time.time() < deadline:
                info(f"waiting for the new instance profile to propagate before {what}...")
                time.sleep(5)
                continue
            raise


def associate_profile(ec2, instance_id, profile_name):
    _retry_on_invalid_profile(
        lambda: ec2.associate_iam_instance_profile(
            IamInstanceProfile={"Name": profile_name}, InstanceId=instance_id
        ),
        "associating",
    )
    ok(f"associated instance profile '{profile_name}' with {instance_id}")


def handle_iam(session, ec2, instance, args, result):
    iam = session.client("iam")
    instance_id = instance["InstanceId"]
    profile_assoc = instance.get("IamInstanceProfile")

    if profile_assoc:
        profile_arn = profile_assoc["Arn"]
        role_name = role_name_from_profile_arn(iam, profile_arn)
        if role_name and role_has_ssm_policy(iam, role_name):
            ok("instance already has an IAM role with SSM permissions")
            result["iam"] = "ok"
            return True

        warn(f"attached role '{role_name}' lacks AmazonSSMManagedInstanceCore.")
        decision = choose(
            "How should we grant SSM permissions?",
            [
                ("attach", f"Attach the SSM policy to the existing role '{role_name}'"),
                ("dedicated", f"Create a dedicated role '{args.role_name}' and replace the association"),
            ],
            args,
        )
        if decision is None:
            info(f"[dry-run] would grant SSM permissions to {instance_id}")
            result["iam"] = "dry-run"
            return True
        if decision == "attach":
            if not confirm(f"Attach SSM policy to role '{role_name}'?", args):
                fail("declined - cannot continue without SSM permissions")
                result["iam"] = "declined"
                return False
            attach_ssm_policy(iam, role_name)
            result["iam"] = "fixed"
            return True
    else:
        warn("instance has NO IAM instance profile attached.")
        if not confirm(
            f"Create role+profile '{args.role_name}' and attach it to {instance_id}?", args
        ):
            fail("declined - cannot continue without an IAM role")
            result["iam"] = "declined"
            return False

    if args.dry_run:
        info(f"[dry-run] would create role/profile '{args.role_name}' and associate it")
        result["iam"] = "dry-run"
        return True
    profile_name = ensure_role_and_profile(iam, args.role_name)
    if profile_assoc:
        try:
            assoc_id = _profile_association_id(ec2, instance_id)
            if assoc_id:
                _retry_on_invalid_profile(
                    lambda: ec2.replace_iam_instance_profile_association(
                        IamInstanceProfile={"Name": profile_name},
                        AssociationId=assoc_id,
                    ),
                    "replacing the association",
                )
                ok(f"replaced instance profile association with '{profile_name}'")
        except ClientError as exc:
            fail(f"could not replace profile association: {exc}")
            result["iam"] = "error"
            return False
    else:
        associate_profile(ec2, instance_id, profile_name)
    result["iam"] = "fixed"
    return True


def _profile_association_id(ec2, instance_id):
    resp = ec2.describe_iam_instance_profile_associations(
        Filters=[{"Name": "instance-id", "Values": [instance_id]}]
    )
    for assoc in resp.get("IamInstanceProfileAssociations", []):
        if assoc.get("State") in ("associated", "associating"):
            return assoc["AssociationId"]
    return None


def check_network(ec2, instance, result):
    vpc_id = instance.get("VpcId")
    subnet_id = instance.get("SubnetId")
    has_public_ip = bool(instance.get("PublicIpAddress"))

    if not vpc_id:
        warn("instance is not in a VPC - skipping network check")
        result["network"] = "skipped"
        return

    routes = _subnet_routes(ec2, vpc_id, subnet_id)
    has_igw = any(
        r.get("GatewayId", "").startswith("igw-") and r.get("DestinationCidrBlock") == "0.0.0.0/0"
        for r in routes
    )
    has_nat = any(
        r.get("NatGatewayId") and r.get("DestinationCidrBlock") == "0.0.0.0/0"
        for r in routes
    )
    endpoints = _ssm_endpoints(ec2, vpc_id)

    if has_public_ip and has_igw:
        ok("public IP + internet gateway route - agent can reach SSM endpoints")
        result["network"] = "ok"
    elif has_nat:
        ok("private subnet with NAT route - agent can reach SSM endpoints")
        result["network"] = "ok"
    elif endpoints >= 3:
        ok("VPC interface endpoints for ssm/ssmmessages/ec2messages present")
        result["network"] = "ok"
    else:
        warn("No obvious network path to SSM endpoints (ssm, ssmmessages, ec2messages).")
        info("The agent needs outbound 443 via public IP+IGW, a NAT gateway, or VPC")
        info("interface endpoints. The instance may stay Offline until this is fixed.")
        result["network"] = "warn"


def _subnet_routes(ec2, vpc_id, subnet_id):
    rts = ec2.describe_route_tables(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    ).get("RouteTables", [])
    # Prefer the route table explicitly associated with the subnet.
    for rt in rts:
        for assoc in rt.get("Associations", []):
            if assoc.get("SubnetId") == subnet_id:
                return rt.get("Routes", [])
    # Fall back to the VPC main route table.
    for rt in rts:
        for assoc in rt.get("Associations", []):
            if assoc.get("Main"):
                return rt.get("Routes", [])
    return []


def _ssm_endpoints(ec2, vpc_id):
    eps = ec2.describe_vpc_endpoints(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    ).get("VpcEndpoints", [])
    wanted = {"ssm", "ssmmessages", "ec2messages"}
    found = set()
    for ep in eps:
        for w in wanted:
            if ep.get("ServiceName", "").endswith(f".{w}"):
                found.add(w)
    return len(found)


def wait_for_managed(ssm, instance_id, args, result):
    if args.dry_run:
        info("[dry-run] would poll for the instance to come Online")
        result["managed"] = "dry-run"
        return False
    info("No reboot needed - the agent picks up new credentials from instance")
    info(f"metadata automatically. Waiting up to {args.wait_timeout}s for it to register...")
    deadline = time.time() + args.wait_timeout
    while time.time() < deadline:
        if is_managed(ssm, instance_id):
            ok("instance is now Online in Systems Manager")
            result["managed"] = "online"
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
        print("  ...still waiting")
    fail("instance did not come Online within the timeout.")
    info("IAM and network were handled, so the cause is the agent itself: either")
    info("not installed/running, or installed but stuck in a long registration")
    info("back-off (common on instances that ran a long time with no IAM role).")
    info("It will often register on its own once the back-off elapses (can be")
    info("tens of minutes). To finish immediately, restart the agent or reboot:")
    info("  Linux:   sudo snap restart amazon-ssm-agent   # or: systemctl restart amazon-ssm-agent")
    info("  Windows: Restart-Service AmazonSSMAgent")
    info("  Or:      aws ec2 reboot-instances --instance-ids <id>")
    info("  Or:      re-run with a longer --wait-timeout to keep polling.")
    info(f"Install / troubleshooting guide: {DOC_AGENT_INSTALL}")
    result["managed"] = "timeout"
    return False


def test_session(instance_id, args, result):
    if not shutil.which("session-manager-plugin"):
        warn("The Session Manager plugin is not installed locally.")
        info(f"Install it to open shells from your machine: {DOC_PLUGIN}")
        result["session"] = "no-plugin"
        return
    if not confirm(f"Open an interactive SSM session to {instance_id} now?", args):
        info("skipped interactive session")
        result["session"] = "skipped"
        return
    cmd = ["aws", "ssm", "start-session", "--target", instance_id]
    if args.profile:
        cmd += ["--profile", args.profile]
    if args.region:
        cmd += ["--region", args.region]
    info(f"launching: {' '.join(cmd)}")
    rc = subprocess.call(cmd)
    result["session"] = "opened" if rc == 0 else f"exit-{rc}"


def process_instance(session, ec2, ssm, instance_id, args):
    result = {"instance": instance_id, "os": "-", "iam": "-", "network": "-",
              "managed": "-", "session": "-"}
    header(f"=== {instance_id} ===")

    try:
        instance = describe_instance(ec2, instance_id)
    except ClientError as exc:
        fail(f"could not describe instance: {exc}")
        result["managed"] = "error"
        return result

    state = instance.get("State", {}).get("Name")
    if state != "running":
        warn(f"instance state is '{state}' - it should be 'running' for SSM")

    check_os(ec2, instance, result)

    if is_managed(ssm, instance_id):
        ok("instance is already managed by SSM (Online)")
        result["iam"] = "ok"
        result["network"] = "ok"
        result["managed"] = "online"
        test_session(instance_id, args, result)
        return result

    if not handle_iam(session, ec2, instance, args, result):
        return result

    if args.no_network_check:
        info("network check skipped (--no-network-check)")
    else:
        check_network(ec2, instance, result)

    if wait_for_managed(ssm, instance_id, args, result):
        test_session(instance_id, args, result)
    return result


def print_summary(results):
    header("Summary")
    cols = ["instance", "os", "iam", "network", "managed", "session"]
    widths = {c: max(len(c), *(len(str(r[c])) for r in results)) for c in cols}
    line = "  " + "  ".join(c.ljust(widths[c]) for c in cols)
    print(line)
    print("  " + "  ".join("-" * widths[c] for c in cols))
    for r in results:
        print("  " + "  ".join(str(r[c]).ljust(widths[c]) for c in cols))


HELP_EPILOG = textwrap.dedent(
    f"""
    ───────────────────────────────────────────────────────────────────────
    WHAT IT DOES
      Onboards one or more EC2 instances to AWS Systems Manager Session
      Manager so you can open a shell WITHOUT SSH, open ports, or key pairs.
      It diagnoses first (read-only), then fixes only what you approve.

    THE SSM TRIAD - three things must all be true, or the instance stays Offline
      1. AGENT    The SSM agent must be installed AND running on the instance.
                  This cannot be checked remotely: if the agent is missing,
                  the instance simply never appears in SSM. ssmify handles
                  IAM and network first, then concludes "agent" by elimination.
      2. IAM      The instance needs an instance profile whose role has the
                  AmazonSSMManagedInstanceCore policy. ssmify can attach the
                  policy to an existing role, or create a dedicated role+profile
                  ({DEFAULT_ROLE_NAME} by default) - it always asks first.
      3. NETWORK  The agent must reach ssm, ssmmessages and ec2messages over
                  outbound 443: via a public IP + internet gateway, a NAT
                  gateway, or VPC interface endpoints. ssmify warns if none
                  of these paths exist.

    WHY NO REBOOT IS NEEDED
      The SSM agent does not hold a static credential - it reads temporary
      credentials from the EC2 Instance Metadata Service (IMDS), which serves
      whatever instance profile is currently associated. After ssmify attaches
      or associates IAM, those credentials appear within a minute or two and
      the agent registers on its own retry cycle. ssmify polls for this.
      If you have shell access and don't want to wait, you may restart the
      agent (this is optional, never required):
        Linux:    sudo systemctl restart amazon-ssm-agent
        Windows:  Restart-Service AmazonSSMAgent

    OS SUPPORT (agent preinstalled on official AWS AMIs)
      Amazon Linux 2 / 2023, recent Ubuntu LTS, Windows Server 2016+,
      macOS, SLES. Families like RHEL, CentOS, Debian, Rocky and Alma usually
      need a manual agent install - ssmify warns and links the docs.

    EXAMPLES
      # Diagnose and onboard a single instance using a named profile
      ssmify.py --instances i-0123456789abcdef0 --profile prod --region eu-west-1

      # Several instances, accept all prompts (CI / unattended)
      ssmify.py --instances i-aaa i-bbb i-ccc --assume-yes

      # See what it WOULD do without changing anything
      ssmify.py --instances i-aaa --dry-run

      # Use a custom IAM role name and skip the network check
      ssmify.py --instances i-aaa --role-name my-ssm-role --no-network-check

    TROUBLESHOOTING
      Instance never comes Online -> agent likely missing/stopped: {DOC_AGENT_INSTALL}
      "session-manager-plugin not found" -> {DOC_PLUGIN}
      General agent docs -> {DOC_AGENT}
    ───────────────────────────────────────────────────────────────────────
    """
)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ssmify.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Get EC2 instances ready for SSM Session Manager (no SSH needed).",
        epilog=HELP_EPILOG,
    )
    parser.add_argument(
        "--version", "-V", action="version", version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--instances", "-i", nargs="+", required=True, metavar="INSTANCE_ID",
        help="one or more EC2 instance IDs to onboard (space separated)",
    )
    parser.add_argument(
        "--profile", "-p", default=None,
        help="AWS credentials/config profile to use (default: standard AWS chain)",
    )
    parser.add_argument(
        "--region", "-r", default=None,
        help="AWS region (default: from profile/environment)",
    )
    parser.add_argument(
        "--role-name", default=DEFAULT_ROLE_NAME, metavar="NAME",
        help=f"name for the IAM role/instance profile to create (default: {DEFAULT_ROLE_NAME})",
    )
    parser.add_argument(
        "--assume-yes", "-y", action="store_true",
        help="answer 'yes' to every confirmation (unattended runs)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="show what would be done without calling any mutating AWS API",
    )
    parser.add_argument(
        "--no-network-check", action="store_true",
        help="skip the VPC/route/endpoint reachability check",
    )
    parser.add_argument(
        "--wait-timeout", type=int, default=POLL_TIMEOUT_SECONDS, metavar="SECONDS",
        help=f"how long to poll for the instance to come Online (default: {POLL_TIMEOUT_SECONDS}s). "
             "Instances that ran a long time without an IAM role can take 10+ minutes to register.",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        ec2 = session.client("ec2")
        ssm = session.client("ssm")
        sts = session.client("sts")
        identity = sts.get_caller_identity()
    except ProfileNotFound as exc:
        fail(str(exc))
        return 2
    except NoCredentialsError:
        fail("no AWS credentials found - configure a profile or environment variables")
        return 2
    except ClientError as exc:
        fail(f"failed to authenticate: {exc}")
        return 2

    region = session.region_name or "(default)"
    print(f"Account {identity['Account']} | region {region} | {len(args.instances)} instance(s)")
    if args.dry_run:
        warn("DRY-RUN: no changes will be made")

    results = []
    for instance_id in args.instances:
        try:
            results.append(process_instance(session, ec2, ssm, instance_id, args))
        except Exception as exc:  # keep going to the next instance
            fail(f"unexpected error on {instance_id}: {exc}")
            results.append({"instance": instance_id, "os": "-", "iam": "-",
                            "network": "-", "managed": "error", "session": "-"})

    print_summary(results)
    online = sum(1 for r in results if r["managed"] in ("online",))
    return 0 if online == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
