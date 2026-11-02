"""
lambda_ec2_control.py
=====================
EC2 START / TERMINATE controller for Momentum Terminal.
"""

import json
import os
import logging
import urllib.request
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError

# ── Logger ────────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ── Config ────────────────────────────────────────────────────────────────────
REGION           = os.environ.get("REGION",           "ap-south-1")
ELASTIC_IP_ALLOC = "eipalloc-00d251a05a1a6d67c"  
ELASTIC_IP       = "65.0.146.148"                

SSM_LT_KEY       = os.environ.get("SSM_LT_KEY",       "/trading-app-mtf/ec2/launch_template_id_toggle")
SSM_INSTANCE_KEY = os.environ.get("SSM_INSTANCE_KEY", "/trading-app-mtf/ec2/instance_id")

# While the app is still booting, report "pending" so the UI shows "Starting…".
# The UI stops polling ~200s after ON, so after BOOT_GRACE_SECS report "running"
# regardless (app_ready tells the truth either way).
HEALTH_URL       = os.environ.get("HEALTH_URL", f"http://{ELASTIC_IP}:5000/health")
BOOT_GRACE_SECS  = int(os.environ.get("BOOT_GRACE_SECS", "180"))

ec2 = boto3.client("ec2",  region_name=REGION)
ssm = boto3.client("ssm",  region_name=REGION)

# Leave this empty. AWS Function URL CORS config will inject these headers now!
CORS_HEADERS = {
    "Content-Type": "application/json"
}

def _resp(code, body):
    return {
        "statusCode": code,
        "headers":    CORS_HEADERS,
        "body":       json.dumps(body),
    }

def _ok(b):  return _resp(200, b)
def _err(m): return _resp(500, {"success": False, "error": m})
def _bad(m): return _resp(400, {"success": False, "error": m})


# ── SSM helpers ───────────────────────────────────────────────────────────────
def get_launch_template_id():
    try:
        response = ssm.get_parameter(Name=SSM_LT_KEY)
        return response["Parameter"]["Value"]
    except Exception as e:
        logger.error(f"❌ Failed to fetch Launch Template ID: {e}")
        return None

def get_saved_instance_id():
    try:
        response = ssm.get_parameter(Name=SSM_INSTANCE_KEY)
        return response["Parameter"]["Value"].strip()
    except Exception:
        return None

def save_instance_id(instance_id):
    try:
        ssm.put_parameter(Name=SSM_INSTANCE_KEY, Value=instance_id, Type="String", Overwrite=True)
    except Exception as e:
        logger.error(f"❌ Failed to save instance_id to SSM: {e}")

def clear_instance_id():
    try:
        ssm.put_parameter(Name=SSM_INSTANCE_KEY, Value="", Type="String", Overwrite=True)
    except Exception:
        pass


# ── EC2 helpers ───────────────────────────────────────────────────────────────
def describe_instance(instance_id):
    try:
        r = ec2.describe_instances(InstanceIds=[instance_id])

        reservations = r.get("Reservations", [])

        if not reservations:
            logger.warning(
                f"No reservations found for instance {instance_id}"
            )
            return None

        instances = reservations[0].get("Instances", [])

        if not instances:
            logger.warning(
                f"No instance data found for {instance_id}"
            )
            return None

        return instances[0]

    except ClientError as e:
        code = e.response["Error"]["Code"]

        if code in (
            "InvalidInstanceID.NotFound",
            "InvalidInstanceID.Malformed",
        ):
            logger.warning(
                f"Instance {instance_id} no longer exists"
            )
            return None

        raise

def get_state(instance_id):
    inst = describe_instance(instance_id)
    if not inst:
        return "not_found"
    return inst["State"]["Name"]

def ensure_elastic_ip(instance_id):
    try:
        addrs = ec2.describe_addresses(AllocationIds=[ELASTIC_IP_ALLOC])
        addr  = addrs["Addresses"][0] if addrs["Addresses"] else {}
        if addr.get("InstanceId", "") == instance_id:
            return {"attached": True}
        ec2.associate_address(InstanceId=instance_id, AllocationId=ELASTIC_IP_ALLOC, AllowReassociation=True)
        return {"attached": True}
    except Exception as e:
        logger.error(f"❌ EIP error: {e}")
        return {"attached": False}

def get_eip_instance_id():
    """Instance currently holding the Elastic IP (e.g. one launched by the schedule)."""
    try:
        addrs = ec2.describe_addresses(AllocationIds=[ELASTIC_IP_ALLOC])["Addresses"]
        return (addrs[0].get("InstanceId") or None) if addrs else None
    except Exception as e:
        logger.error(f"❌ EIP lookup error: {e}")
        return None

DEAD_STATES = ("terminated", "not_found", "shutting-down")

def app_is_healthy():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as r:
            return r.status == 200 and b"ok" in r.read(200)
    except Exception:
        return False

def instance_age_secs(inst):
    lt = inst.get("LaunchTime")
    if not lt:
        return 10**9
    return (datetime.now(timezone.utc) - lt).total_seconds()

def get_lt_instance_id():
    """Newest live instance launched from the toggle's launch template
    (covers a just-launched instance that has no EIP yet)."""
    lt_id = get_launch_template_id()
    if not lt_id:
        return None
    try:
        r = ec2.describe_instances(Filters=[
            {"Name": "tag:aws:ec2launchtemplate:id", "Values": [lt_id]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ])
        insts = [i for res in r.get("Reservations", []) for i in res.get("Instances", [])]
        if not insts:
            return None
        return max(insts, key=lambda i: i["LaunchTime"])["InstanceId"]
    except Exception as e:
        logger.error(f"❌ LT instance lookup error: {e}")
        return None

def resolve_instance_id():
    """
    Live instance to report/control, in order:
      1. saved SSM instance_id (if still alive)
      2. instance holding the Elastic IP (scheduled launches)
      3. newest instance from the launch template (just launched, no EIP yet)
    Returns None when nothing is alive.
    """
    saved_id = get_saved_instance_id()
    if saved_id and get_state(saved_id) not in DEAD_STATES:
        return saved_id
    for found in (get_eip_instance_id(), get_lt_instance_id()):
        if found and get_state(found) not in DEAD_STATES:
            if found != saved_id:
                logger.info(f"🔁 Adopting instance {found} (saved was {saved_id})")
                save_instance_id(found)
            return found
    return None

def launch_ec2():
    lt_id = get_launch_template_id()
    if not lt_id:
        return {"status": "failed", "error": "Launch Template ID not found"}
    try:
        response = ec2.run_instances(LaunchTemplate={"LaunchTemplateId": lt_id, "Version": "$Latest"}, MinCount=1, MaxCount=1)
        return {"status": "success", "instance_id": response["Instances"][0]["InstanceId"]}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


# ── ACTIONS ───────────────────────────────────────────────────────────────────
def action_status():
    instance_id = resolve_instance_id()
    if not instance_id:
        return _ok({"success": True, "state": "stopped", "instance_id": None, "elastic_ip": ELASTIC_IP})

    inst  = describe_instance(instance_id)
    state = inst["State"]["Name"] if inst else "not_found"
    if state in DEAD_STATES:
        return _ok({"success": True, "state": "stopped", "instance_id": None, "elastic_ip": ELASTIC_IP})

    app_ready = False
    if state == "running":
        ensure_elastic_ip(instance_id)
        app_ready = app_is_healthy()
        if not app_ready and instance_age_secs(inst) < BOOT_GRACE_SECS:
            state = "pending"          # EC2 up, app still booting → UI shows "Starting…"

    return _ok({"success": True, "state": state, "instance_id": instance_id,
                "app_ready": app_ready, "elastic_ip": ELASTIC_IP})


def action_start():
    """
    ON: launch a NEW instance from the launch template.
    If one is already running/pending, return it instead of launching a second.
    A leftover stopped instance is terminated first.
    The EIP is attached by the template's user data and by action_status()
    once the instance reaches "running" (Lambda timeout is short — no waiting here).
    """
    existing = resolve_instance_id()
    if existing:
        state = get_state(existing)
        if state in ("running", "pending"):
            return _ok({"success": True, "action": "start", "instance_id": existing, "state": state, "already": True, "elastic_ip": ELASTIC_IP})
        try:
            ec2.terminate_instances(InstanceIds=[existing])
        except ClientError as e:
            logger.warning(f"Could not terminate leftover {existing}: {e}")

    result = launch_ec2()
    if result["status"] != "success":
        return _err(result.get("error", "Launch failed"))

    new_id = result["instance_id"]
    save_instance_id(new_id)
    return _ok({"success": True, "action": "start", "instance_id": new_id, "state": "pending", "already": False, "elastic_ip": ELASTIC_IP})


def action_terminate():
    instance_id = resolve_instance_id()
    if not instance_id:
        return _ok({"success": True, "action": "terminate", "state": "stopped", "already": True})

    state = get_state(instance_id)
    if state in ("not_found", "terminated", "shutting-down"):
        clear_instance_id()
        return _ok({"success": True, "action": "terminate", "state": "stopped", "already": True})

    try:
        ec2.terminate_instances(InstanceIds=[instance_id])
    except ClientError as e:
        return _err(f"Terminate operations failed: {e}")

    clear_instance_id()
    return _ok({"success": True, "action": "terminate", "instance_id": instance_id, "state": "stopping"})


# ── Lambda Handler ────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    logger.info(f"📨 Event: {json.dumps(event, default=str)}")
    
    method = (event.get("requestContext", {}).get("http", {}).get("method", "") or event.get("httpMethod", "GET")).upper()

    # AWS Console handles OPTIONS now, but we keep this as a backend backup safety layer
    if method == "OPTIONS":
        return {"statusCode": 200, "headers": {"Access-Control-Allow-Origin": "*"}, "body": ""}

    action = ""
    query_params = event.get("queryStringParameters") or {}
    if query_params.get("action"):
        action = query_params.get("action").lower().strip()

    if method == "POST":
        raw = event.get("body") or "{}"
        try:
            body = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(body, dict) and body.get("action"):
                action = body.get("action").lower().strip()
        except json.JSONDecodeError:
            return _bad("Invalid JSON body")

    if not action:
        action = "status"

    if action == "status":
        return action_status()
    elif action == "start":
        return action_start()
    elif action in ("terminate", "stop"):
        # UI toggle OFF → terminate the EC2.
        return action_terminate()
    else:
        return _bad(f"Unknown action '{action}'")