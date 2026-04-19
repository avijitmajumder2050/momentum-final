"""
ssm_config.py — AWS SSM Parameter Store loader
"""
import os, logging
import boto3
from botocore.exceptions import ClientError

log        = logging.getLogger(__name__)
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
PREFIX     = "/momentum-watchlist"

PARAM_MAP = {
    f"{PREFIX}/apiKey":          "apiKey",
    f"{PREFIX}/totpKey":         "totpKey",
    f"{PREFIX}/userid":          "userid",
    f"{PREFIX}/pin":             "pin",
    f"{PREFIX}/S3_BUCKET":       "S3_BUCKET",
    f"{PREFIX}/trading-capital": "TRADING_CAPITAL",
    f"{PREFIX}/max-loss":        "MAX_LOSS",
    f"{PREFIX}/github_repo":     "GITHUB_REPO",
}
SECURE = {f"{PREFIX}/apiKey", f"{PREFIX}/totpKey",
          f"{PREFIX}/userid", f"{PREFIX}/pin"}


def load_ssm_to_env(region: str = AWS_REGION) -> dict:
    client = boto3.client("ssm", region_name=region)
    resp   = client.get_parameters(Names=list(PARAM_MAP.keys()), WithDecryption=True)
    found  = {p["Name"]: p["Value"] for p in resp["Parameters"]}
    loaded = {}
    for path, env_key in PARAM_MAP.items():
        if path in found:
            os.environ[env_key] = found[path]
            loaded[env_key]     = found[path]
    missing = [k for k in PARAM_MAP if k not in found]
    if missing:
        log.warning("[SSM] Missing: %s", missing)
    log.info("[SSM] Loaded %d params", len(loaded))
    return loaded


def bootstrap() -> None:
    if os.getenv("USE_SSM", "true").lower() == "true":
        try:
            load_ssm_to_env(); return
        except Exception as e:
            log.warning("[SSM] Failed (%s) — falling back to .env", e)
    from dotenv import load_dotenv
    load_dotenv()
    log.info("[SSM] Using .env")


def push_env_to_ssm(region: str = AWS_REGION) -> None:
    from dotenv import load_dotenv
    load_dotenv()
    client = boto3.client("ssm", region_name=region)
    for path, env_key in PARAM_MAP.items():
        val = os.getenv(env_key)
        if not val:
            print(f"  SKIP {env_key}")
            continue
        ptype = "SecureString" if path in SECURE else "String"
        client.put_parameter(Name=path, Value=val, Type=ptype,
                             Overwrite=True, Description=f"Momentum/{env_key}")
        print(f"  OK   {path} [{ptype}]")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    push_env_to_ssm()
