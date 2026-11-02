"""
lambda_ec2_control.py
=====================
EC2 START / TERMINATE controller for Momentum Terminal.
"""

import json
import os
import logging
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

def resolve_instance_id():
    """
    Saved SSM instance_id if it is still alive; otherwise fall back to the
    instance holding the Elastic IP and re-save it, so instances started
    outside this Lambda (scheduled launches) are still tracked.
    """
    saved_id = get_saved_instance_id()
    if saved_id and get_state(saved_id) not in ("terminated", "not_found", "shutting-down"):
        return saved_id
    eip_id = get_eip_instance_id()
    if eip_id and get_state(eip_id) not in ("terminated", "not_found", "shutting-down"):
        if eip_id != saved_id:
            logger.info(f"🔁 Adopting EIP instance {eip_id} (saved was {saved_id})")
            save_instance_id(eip_id)
        return eip_id
    return saved_id or None

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

    state = get_state(instance_id)
    if state in ("terminated", "not_found"):
        return _ok({"success": True, "state": "stopped", "instance_id": None, "elastic_ip": ELASTIC_IP})

    if state == "running":
        ensure_elastic_ip(instance_id)

    return _ok({"success": True, "state": state, "instance_id": instance_id, "elastic_ip": ELASTIC_IP})


def action_start():
    """
    ON: start the existing (stopped) instance — same disk, same EIP.
    Only launches a fresh instance from the template when none exists.
    """
    saved_id = resolve_instance_id()
    if saved_id:
        state = get_state(saved_id)
        if state in ("running", "pending"):
            return _ok({"success": True, "action": "start", "instance_id": saved_id, "state": state, "already": True, "elastic_ip": ELASTIC_IP})
        if state == "stopping":
            return _bad("Instance is still stopping — try again in a minute")
        if state == "stopped":
            # Lambda timeout is 3s — don't wait. UI polls ?action=status, which
            # re-attaches the EIP via ensure_elastic_ip() once state is "running".
            try:
                ec2.start_instances(InstanceIds=[saved_id])
            except ClientError as e:
                return _err(f"Start failed: {e}")
            return _ok({"success": True, "action": "start", "instance_id": saved_id, "state": "pending", "already": False, "elastic_ip": ELASTIC_IP})

    result = launch_ec2()
    if result["status"] != "success":
        return _err(result.get("error", "Launch failed"))

    new_id = result["instance_id"]
    save_instance_id(new_id)
    return _ok({"success": True, "action": "start", "instance_id": new_id, "state": "pending", "already": False, "elastic_ip": ELASTIC_IP})


def action_stop():
    """OFF: stop (not terminate) the instance. Disk, EIP and instance_id are kept."""
    instance_id = resolve_instance_id()
    if not instance_id:
        return _ok({"success": True, "action": "stop", "state": "stopped", "already": True})

    state = get_state(instance_id)
    if state in ("stopped", "stopping", "not_found", "terminated", "shutting-down"):
        return _ok({"success": True, "action": "stop", "instance_id": instance_id, "state": "stopped", "already": True})

    try:
        ec2.stop_instances(InstanceIds=[instance_id])
    except ClientError as e:
        return _err(f"Stop failed: {e}")

    return _ok({"success": True, "action": "stop", "instance_id": instance_id, "state": "stopping"})


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
    elif action in ("stop", "terminate"):
        # UI toggle OFF sends "terminate" — now a reversible stop.
        return action_stop()
    elif action == "terminate_hard":
        return action_terminate()
    else:
        return _bad(f"Unknown action '{action}'")