import os
import boto3
from dotenv import load_dotenv

REGION = "ap-south-1"
ENV_FILE = "backend/.env"

# Load .env into environment
load_dotenv(ENV_FILE)

ssm = boto3.client("ssm", region_name=REGION)


def debug_env():
    print("\n=== ENV DEBUG ===")
    keys = ["apiKey", "totpKey", "userid", "pin", "GITHUB_REPO"]

    for k in keys:
        print(f"{k} = {os.getenv(k)}")


def put_param(name, value, secure=True):
    """
    Push parameter to AWS SSM
    """
    if not name.startswith("/"):
        raise ValueError(f"Invalid SSM name (must start with /): {name}")

    if value is None or value == "":
        raise ValueError(f"Empty value for {name}")

    print(f"PUSH → {name}")

    ssm.put_parameter(
        Name=name,
        Value=str(value),
        Type="SecureString" if secure else "String",
        Overwrite=True
    )


def main():
    print("=== Loading .env ===")

    debug_env()

    print("\n=== PUSHING TO SSM ===")

    put_param("/momentum-watchlist/apiKey", os.getenv("apiKey"))
    put_param("/momentum-watchlist/totpKey", os.getenv("totpKey"))
    put_param("/momentum-watchlist/userid", os.getenv("userid"))
    put_param("/momentum-watchlist/pin", os.getenv("pin"))

    put_param("/momentum-watchlist/S3_BUCKET", "momentum-watchlist-bucket", secure=False)
    put_param("/momentum-watchlist/trading-capital", os.getenv("TRADING_CAPITAL", "100000"), secure=False)
    put_param("/momentum-watchlist/max-loss", os.getenv("MAX_LOSS", "1000"), secure=False)
    put_param("/momentum-watchlist/github_repo", os.getenv("GITHUB_REPO", ""), secure=False)

    print("\n=== DONE ===")


if __name__ == "__main__":
    main()