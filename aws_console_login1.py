"""
1) Open the AWS Management Console already signed in (access key + secret key from .env),
   landing on the security group whose ID is in .env.
2) Whitelist the IPs saved by the WhatsApp scraper (ip_port.json) as inbound rules of that
   security group, one by one, using the EC2 API (AuthorizeSecurityGroupIngress), and print
   a status for every IP.

The console does not accept an access key directly (keys are for the API / CLI / SDK), so the
login uses the AWS "federation" sign-in: STS temporary credentials -> sign-in token ->
one-time login URL. The whitelisting itself does not click through the console: it calls the
same API that the "Edit inbound rules" screen uses, which is faster and gives a clear result.

Usage:
    python3 aws_console_login.py                    # open console, then whitelist (no questions asked)
    python3 aws_console_login.py --ask              # show the rules and ask 'yes' before adding
    python3 aws_console_login.py --dry-run          # show what WOULD be added, change nothing
    python3 aws_console_login.py --login-only       # only open the console
    python3 aws_console_login.py --whitelist-only   # only whitelist, no browser

.env variables (nothing is hard-coded, nothing secret is printed):
    AWS_ACCESS_KEY_ID          required
    AWS_SECRET_ACCESS_KEY      required
    AWS_SESSION_TOKEN          optional (only if your keys are temporary)
    AWS_DEFAULT_REGION         region of the security group (or AWS_REGION), default ap-south-1
    AWS_SECURITY_GROUP_ID      sg-... of the group to open and to add rules to
    AWS_SECURITY_GROUP_NAME    optional safety check, e.g. 443-office (abort if the group differs)
    AWS_DEFAULT_PORT           optional, port used for JSON entries whose port is null
                               (if not set, those entries are skipped)
    WHITELIST_JSON             optional, default ip_port.json
    AWS_WHITELIST_CONFIRM      optional, true = ask 'yes' before adding (same as --ask)
    AWS_ROLE_ARN               optional, assume this role for login and whitelisting
    HEADLESS                   true = do not open any browser, only whitelist through the API
                               false (or missing) = open the console first
    AWS_SG_VIEW                optional, "list" opens the filtered list instead of the details page
    AWS_CONSOLE_SESSION_SECS   optional, console session length, default 3600

Install:  pip install boto3
The .env file (next to this script) is read by the script itself, python-dotenv is not needed.
"""
import argparse
from datetime import date
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request
import webbrowser

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# --------------------------------------------------------------------------
# .env loading (built in, no python-dotenv needed)
# The old code did "try: import dotenv ... except ImportError: pass", so when
# python-dotenv was not installed in the venv the .env file was silently ignored.
# --------------------------------------------------------------------------
ENV_ALIASES = {
    "AWS_ACCESS_KEY_ID": ["AWS_ACCESS_KEY", "ACCESS_KEY_ID", "ACCESS_KEY"],
    "AWS_SECRET_ACCESS_KEY": ["AWS_SECRET_KEY", "SECRET_ACCESS_KEY", "SECRET_KEY"],
}


def load_env_file():
    """Read KEY=VALUE lines from a .env next to this script (or in the current folder).
    Returns (path or None, list of variable NAMES found). Values are never printed."""
    for path in (Path(__file__).resolve().parent / ".env", Path.cwd() / ".env"):
        if not path.is_file():
            continue
        names = []
        for line in path.read_text(encoding="utf-8-sig").splitlines():  # utf-8-sig drops a BOM
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            closing = value.find(value[0], 1) if value and value[0] in "\"'" else -1
            if closing != -1:
                value = value[1:closing]               # quoted value (anything after the quote is a comment)
            else:
                value = value.split(" #")[0].strip()   # unquoted value: drop a trailing "# comment"
            names.append(key)
            if not os.environ.get(key):                # a real environment value wins
                os.environ[key] = value
        return path, names
    return None, []


def apply_aliases():
    """Accept common alternative names and map them to the names boto3 expects."""
    for standard, alternatives in ENV_ALIASES.items():
        if os.environ.get(standard):
            continue
        for alt in alternatives:
            if os.environ.get(alt):
                os.environ[standard] = os.environ[alt]
                print(f"Note: using {alt} as {standard}")
                break


ENV_FILE, ENV_NAMES = load_env_file()
apply_aliases()

SCRIPT_VERSION = "2.6 (automatic whitelist, HEADLESS=true/false in .env, scraper hook)"
REGION = os.getenv("AWS_DEFAULT_REGION") or os.getenv("AWS_REGION") or "ap-south-1"
SESSION_SECS = int(os.getenv("AWS_CONSOLE_SESSION_SECS", "3600"))
ROLE_ARN = os.getenv("AWS_ROLE_ARN")
ISSUER = "scrape-tools"
SIGNIN_URL = "https://signin.aws.amazon.com/federation"
SECURITY_GROUP_ID = (os.getenv("AWS_SECURITY_GROUP_ID") or "").strip()
SG_VIEW = (os.getenv("AWS_SG_VIEW") or "details").strip().lower()

if not SECURITY_GROUP_ID:
    SG_FRAGMENT = "#SecurityGroups:"
elif SG_VIEW == "list":
    SG_FRAGMENT = f"#SecurityGroups:search={SECURITY_GROUP_ID}"
else:
    SG_FRAGMENT = f"#SecurityGroup:securityGroupId={SECURITY_GROUP_ID}"

DESTINATION = f"https://{REGION}.console.aws.amazon.com/ec2/home?region={REGION}{SG_FRAGMENT}"

# --- whitelist settings ---------------------------------------------------
WHITELIST_JSON = (os.getenv("WHITELIST_JSON") or "ip_port.json").strip()
EXPECTED_SG_NAME = (os.getenv("AWS_SECURITY_GROUP_NAME") or "").strip()
ASK_CONFIRM = (os.getenv("AWS_WHITELIST_CONFIRM") or "").strip().lower() in ("1", "true", "yes")
_headless_text = (os.getenv("HEADLESS") or "").strip().lower()
if _headless_text and _headless_text not in ("1", "true", "yes", "on", "0", "false", "no", "off"):
    print(f"Note: HEADLESS='{_headless_text}' is not true or false, using false.")
HEADLESS = _headless_text in ("1", "true", "yes", "on")
_default_port = (os.getenv("AWS_DEFAULT_PORT") or "").strip()
DEFAULT_PORT = int(_default_port) if _default_port.isdigit() else None

FEDERATION_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
}


def check_security_group():
    """Stop early if AWS_SECURITY_GROUP_ID is set but is not a valid ID (sg- + 8 or 17 hex chars)."""
    if not SECURITY_GROUP_ID:
        print("Note: AWS_SECURITY_GROUP_ID is not set in .env, the full Security Groups list will open.")
        return
    if not re.fullmatch(r"sg-([0-9a-f]{8}|[0-9a-f]{17})", SECURITY_GROUP_ID):
        sys.exit(f"Failure: AWS_SECURITY_GROUP_ID '{SECURITY_GROUP_ID}' is not a valid ID "
                 "(it should look like sg-0123456789abcdef0).")
    print(f"Security group from .env: {SECURITY_GROUP_ID}")


def check_env():
    missing = [v for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY") if not os.getenv(v)]
    if ENV_FILE:
        print(f".env loaded from: {ENV_FILE}")
    if not missing:
        check_security_group()
        return

    print(f"Failure: missing environment variable(s): {', '.join(missing)}")
    if ENV_FILE is None:
        print("  No .env file found. Looked in:")
        print(f"    {Path(__file__).resolve().parent / '.env'}")
        print(f"    {Path.cwd() / '.env'}")
    else:
        print(f"  Variable names found in {ENV_FILE.name}: {ENV_NAMES or '(none - file is empty?)'}")
        print("  Expected names: AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY (no spaces, KEY=VALUE per line).")
        print("  Also check that the file is saved and the values are not empty.")
    sys.exit(1)


def get_session_credentials(sts):
    """Return temporary credentials {AccessKeyId, SecretAccessKey, SessionToken}."""
    if os.getenv("AWS_SESSION_TOKEN") and not ROLE_ARN:
        return {
            "AccessKeyId": os.environ["AWS_ACCESS_KEY_ID"],
            "SecretAccessKey": os.environ["AWS_SECRET_ACCESS_KEY"],
            "SessionToken": os.environ["AWS_SESSION_TOKEN"],
        }

    if ROLE_ARN:
        resp = sts.assume_role(
            RoleArn=ROLE_ARN,
            RoleSessionName="console-login",
            DurationSeconds=SESSION_SECS,
        )
        return resp["Credentials"]

    resp = sts.get_federation_token(
        Name="console-login",
        Policy=json.dumps(FEDERATION_POLICY),
        DurationSeconds=max(SESSION_SECS, 900),
    )
    return resp["Credentials"]


def build_login_url(creds):
    session_json = json.dumps({
        "sessionId": creds["AccessKeyId"],
        "sessionKey": creds["SecretAccessKey"],
        "sessionToken": creds["SessionToken"],
    })
    token_req = (
        f"{SIGNIN_URL}?Action=getSigninToken"
        f"&SessionDuration={SESSION_SECS}"
        f"&Session={urllib.parse.quote_plus(session_json)}"
    )
    with urllib.request.urlopen(token_req, timeout=30) as r:
        signin_token = json.loads(r.read())["SigninToken"]

    return (
        f"{SIGNIN_URL}?Action=login"
        f"&Issuer={urllib.parse.quote_plus(ISSUER)}"
        f"&Destination={urllib.parse.quote_plus(DESTINATION)}"
        f"&SigninToken={signin_token}"
    )


def open_console(sts):
    """Open the console in the browser. Returns True on success (never stops the script)."""
    try:
        creds = get_session_credentials(sts)
        login_url = build_login_url(creds)
    except (ClientError, BotoCoreError, OSError, KeyError) as e:
        print(f"Console login: Failure - could not create the console login ({e})")
        return False

    target = SECURITY_GROUP_ID or "Security Groups list"
    print(f"Console login: Success - opening EC2 > Security Groups > {target} ({REGION}) in your browser...")
    if not webbrowser.open(login_url):
        print("Could not open a browser automatically. Open this link yourself "
              "(valid for a few minutes, use it once):")
        print(login_url)
    return True


def find_json_file():
    name = Path(WHITELIST_JSON)
    candidates = [name] if name.is_absolute() else [Path(__file__).resolve().parent / name, Path.cwd() / name]
    return next((p for p in candidates if p.is_file()), None)


def parse_ports(value):
    """Turn 8123, "8123", "80,443 & 8080" or [80, 443] into a list of raw ports ([None] if empty)."""
    if value in (None, "", []):
        return [None]
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    elif isinstance(value, str):
        items = [p for p in re.split(r"[\s,;/&|]+", value) if p and p.lower() != "and"]
    else:
        items = [value]
    return items or [None]


def expand_entry(entry):
    """Return a list of (ip, port, skip_reason), one item for EACH port of the entry.
    skip_reason is None when that IP + port can be whitelisted."""
    if not isinstance(entry, dict):
        return [("?", None, "entry is not an object")]
    raw_ip = str(entry.get("ip", "")).strip()
    if raw_ip.endswith("/32"):
        raw_ip = raw_ip[:-3].strip()
    try:
        ip = ipaddress.IPv4Address(raw_ip)
    except ValueError:
        return [(raw_ip or "?", None, f"invalid IPv4 address '{raw_ip}'")]
    if ip.is_unspecified or ip.is_loopback or ip.is_multicast or ip.is_link_local or ip.is_reserved:
        return [(raw_ip, None, "address not allowed (unspecified / loopback / multicast / reserved)")]

    out = []
    for raw in parse_ports(entry.get("ports", entry.get("port"))):
        if raw in (None, ""):
            if DEFAULT_PORT is None:
                out.append((raw_ip, None,
                            "no port in the message (set AWS_DEFAULT_PORT in .env to use a default)"))
            else:
                out.append((str(ip), DEFAULT_PORT, None))
            continue
        try:
            port = int(raw)
        except (TypeError, ValueError):
            out.append((raw_ip, None, f"invalid port '{raw}'"))
            continue
        if not 1 <= port <= 65535:
            out.append((raw_ip, None, f"port {port} out of range"))
            continue
        out.append((str(ip), port, None))
    return out


def rule_exists(sg, ip, port):
    """True if the group already allows this /32 on this TCP port (or all traffic)."""
    cidr = f"{ip}/32"
    for perm in sg.get("IpPermissions", []):
        proto = perm.get("IpProtocol")
        if proto == "-1":
            covers = True
        elif proto == "tcp":
            covers = perm.get("FromPort", 0) <= port <= perm.get("ToPort", 65535)
        else:
            continue
        if covers and any(r.get("CidrIp") == cidr for r in perm.get("IpRanges", [])):
            return True
    return False


def make_ec2_client(sts):
    if ROLE_ARN:
        c = sts.assume_role(RoleArn=ROLE_ARN, RoleSessionName="sg-whitelist",
                            DurationSeconds=max(SESSION_SECS, 900))["Credentials"]
        return boto3.client("ec2", region_name=REGION,
                            aws_access_key_id=c["AccessKeyId"],
                            aws_secret_access_key=c["SecretAccessKey"],
                            aws_session_token=c["SessionToken"])
    return boto3.client("ec2", region_name=REGION)


def fetch_security_group(ec2):
    """Read the group (for the name check and the duplicate check). None if not allowed."""
    try:
        return ec2.describe_security_groups(GroupIds=[SECURITY_GROUP_ID])["SecurityGroups"][0]
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Error")
        print(f"Note: could not read the security group ({code}). Skipping the pre-check; "
              "AWS will still reject duplicate rules.")
    except (BotoCoreError, IndexError, KeyError) as e:
        print(f"Note: could not read the security group ({e}). Skipping the pre-check.")
    return None


def add_rule(ec2, ip, port):
    """Add one inbound TCP rule. Returns (status, detail)."""
    description = f"WhatsApp CM-Support whitelist {date.today().isoformat()}"
    try:
        ec2.authorize_security_group_ingress(
            GroupId=SECURITY_GROUP_ID,
            IpPermissions=[{
                "IpProtocol": "tcp",
                "FromPort": port,
                "ToPort": port,
                "IpRanges": [{"CidrIp": f"{ip}/32", "Description": description}],
            }],
        )
        return "Added", f"TCP {port} from {ip}/32"
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Error")
        msg = e.response.get("Error", {}).get("Message", "")[:180]
        if code == "InvalidPermission.Duplicate":
            return "Already exists", "rule is already in the security group"
        if code in ("UnauthorizedOperation", "AccessDenied", "AuthFailure"):
            return "Failed", f"not authorized: {msg}"
        return "Failed", f"{code}: {msg}"
    except BotoCoreError as e:
        return "Failed", str(e)[:180]


def confirm(pending, ask):
    """Show the rules about to be added. Only asks 'yes' when --ask / AWS_WHITELIST_CONFIRM is on."""
    print(f"\nAdding {len(pending)} inbound rule(s) to {SECURITY_GROUP_ID} in {REGION}:")
    for p in pending:
        print(f"   TCP {p['port']:<5} from {p['ip']}/32")
    if not ask:
        return True
    if not sys.stdin.isatty():
        print("No terminal to ask for confirmation, so nothing was changed.")
        return False
    return input("Type 'yes' to add these rules: ").strip().lower() == "yes"


def print_report(results):
    print(f"\n{'#':<3} {'IP':<16} {'PORT':<6} {'STATUS':<15} DETAIL")
    print("-" * 78)
    for n, r in enumerate(results, 1):
        print(f"{n:<3} {r['ip']:<16} {str(r['port'] or '-'):<6} {r['status']:<15} {r['detail']}")


def save_status(path, entries, results):
    """Write whitelist_status / whitelist_detail back into the JSON file."""
    by_entry = {}
    for r in results:
        by_entry.setdefault(r["index"], []).append(r)
    for index, rs in by_entry.items():
        if len(rs) == 1:
            entries[index]["whitelist_status"] = rs[0]["status"]
            entries[index]["whitelist_detail"] = rs[0]["detail"]
        else:
            statuses = {r["status"] for r in rs}
            entries[index]["whitelist_status"] = statuses.pop() if len(statuses) == 1 else "Mixed"
            entries[index]["whitelist_detail"] = "; ".join(f"{r['port']}: {r['status']}" for r in rs)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    tmp.replace(path)


def run_whitelist(sts, dry_run=False, ask=False):
    """Whitelist every entry of the JSON file. Returns True when nothing failed."""
    if not SECURITY_GROUP_ID:
        print("Whitelist: Failure - set AWS_SECURITY_GROUP_ID in .env first.")
        return False
    if _default_port and DEFAULT_PORT is None:
        print(f"Note: AWS_DEFAULT_PORT '{_default_port}' is not a number and is ignored.")

    path = find_json_file()
    if path is None:
        print(f"Whitelist: Failure - {WHITELIST_JSON} not found (run the WhatsApp scraper first).")
        return False
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("expected a JSON list")
    except (OSError, ValueError) as e:
        print(f"Whitelist: Failure - could not read {path} ({e})")
        return False
    print(f"\nWhitelist: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} read from {path} "
          "(newest first, processed one by one)")

    ec2 = make_ec2_client(sts)
    sg = fetch_security_group(ec2)
    if sg:
        names = {sg.get("GroupName", "")} | {t.get("Value", "") for t in sg.get("Tags", [])
                                              if t.get("Key") == "Name"}
        label = sg.get("GroupName", "?")
        if EXPECTED_SG_NAME and EXPECTED_SG_NAME not in names:
            print(f"Whitelist: Failure - {SECURITY_GROUP_ID} is '{label}', not '{EXPECTED_SG_NAME}'. "
                  "Nothing was changed.")
            return False
        print(f"Security group: {SECURITY_GROUP_ID} ({label})")

    results, pending, seen = [], [], set()
    for i, entry in enumerate(entries):
        for ip, port, skip in expand_entry(entry):
            res = {"index": i, "ip": ip, "port": port, "status": "", "detail": ""}
            results.append(res)
            if skip:
                res.update(status="Skipped", detail=skip)
            elif (ip, port) in seen:
                res.update(status="Skipped", detail="duplicate entry in the JSON file")
            elif sg and rule_exists(sg, ip, port):
                seen.add((ip, port))
                res.update(status="Already exists", detail="rule is already in the security group")
            else:
                seen.add((ip, port))
                pending.append(res)

    if pending and dry_run:
        for r in pending:
            r.update(status="Would add", detail=f"TCP {r['port']} from {r['ip']}/32 (dry run)")
    elif pending:
        if confirm(pending, ask):
            for r in pending:
                r["status"], r["detail"] = add_rule(ec2, r["ip"], r["port"])
                print(f"  {r['ip']}:{r['port']} -> {r['status']}")
        else:
            for r in pending:
                r.update(status="Cancelled", detail="not confirmed, nothing changed")

    print_report(results)
    count = lambda status: sum(1 for r in results if r["status"] == status)
    print(f"\nWhitelist summary: {count('Added')} added, {count('Already exists')} already existed, "
          f"{count('Skipped')} skipped, {count('Failed')} failed, {count('Cancelled')} cancelled"
          + (f", {count('Would add')} would be added (dry run)" if dry_run else ""))

    if not dry_run:
        try:
            save_status(path, entries, results)
            print(f"Status saved to {path.name} (whitelist_status / whitelist_detail)")
        except OSError as e:
            print(f"Note: could not write the status back to {path.name} ({e})")

    ok = count("Failed") == 0 and count("Cancelled") == 0
    print(f"Whitelist: {'Success' if ok else 'Failure'}"
          + ("" if ok else " - see the FAILED / CANCELLED lines above"))
    return ok


def run_aws_from_scraper(json_file=None, login=True, whitelist=True, dry_run=False, ask=False):
    """Run AWS console login and/or SG whitelisting after WhatsApp + DB save."""
    global WHITELIST_JSON
    if json_file:
        WHITELIST_JSON = str(json_file)

    check_env()
    try:
        sts = boto3.client("sts", region_name=REGION)
        who = sts.get_caller_identity()
    except (ClientError, BotoCoreError) as e:
        print(f"AWS: Failure - credentials rejected ({e})")
        return False

    print(f"AWS credentials valid. Account: {who['Account']}  Identity: {who['Arn']}")
    if who["Arn"].endswith(":root"):
        print("AWS: Failure - do not use root access keys.")
        return False

    login_ok = True
    if login and not HEADLESS:
        login_ok = open_console(sts)
    elif login and HEADLESS:
        print("HEADLESS=true: skipping browser console open.")

    whitelist_ok = True
    if whitelist:
        whitelist_ok = run_whitelist(sts, dry_run=dry_run, ask=ask or ASK_CONFIRM)

    return bool(login_ok and whitelist_ok)


def main():
    parser = argparse.ArgumentParser(description="AWS console login + security group whitelist")
    parser.add_argument("--login-only", action="store_true", help="only open the console")
    parser.add_argument("--whitelist-only", action="store_true", help="only whitelist, no browser")
    parser.add_argument("--dry-run", action="store_true", help="show what would be added, change nothing")
    parser.add_argument("--ask", action="store_true", help="ask 'yes' before adding the rules")
    args = parser.parse_args()
    if args.login_only and args.whitelist_only:
        parser.error("use either --login-only or --whitelist-only, not both")

    print(f"Running aws_console_login.py v{SCRIPT_VERSION}")
    check_env()
    try:
        sts = boto3.client("sts", region_name=REGION)
        who = sts.get_caller_identity()
    except (ClientError, BotoCoreError) as e:
        sys.exit(f"Failure: the access keys were rejected by AWS ({e})")
    print(f"Credentials valid. Account: {who['Account']}  Identity: {who['Arn']}")
    if who["Arn"].endswith(":root"):
        sys.exit("Failure: do not use root access keys. Use an IAM user or a role.")

    login_ok = True
    if args.whitelist_only:
        pass
    elif HEADLESS:
        print("HEADLESS=true: not opening the browser, using the API only.")
    else:
        login_ok = open_console(sts)

    whitelist_ok = True
    if not args.login_only:
        whitelist_ok = run_whitelist(sts, dry_run=args.dry_run, ask=args.ask or ASK_CONFIRM)

    if not (login_ok and whitelist_ok):
        sys.exit(1)


if __name__ == "__main__":
    main()
