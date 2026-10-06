# backend/aws_s3.py
import boto3
import os
import logging
from botocore.exceptions import ClientError

AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")

# Primary bucket from env (if provided)
DEFAULT_BUCKET = os.getenv("S3_BUCKET", "dhan-trading-data")

# Buckets to try
BUCKET_CANDIDATES = [
    DEFAULT_BUCKET,
    "dhan-trading-data",
    "new-dhan-trading-data",
]

s3 = boto3.client("s3", region_name=AWS_REGION)


def get_working_bucket():
    """
    Returns the first bucket that exists and is accessible.
    """
    for bucket in dict.fromkeys(BUCKET_CANDIDATES):   # remove duplicates
        try:
            s3.head_bucket(Bucket=bucket)
            logging.info(f"Using S3 bucket: {bucket}")
            return bucket
        except ClientError:
            continue

    raise RuntimeError(
        f"None of these buckets are accessible: {BUCKET_CANDIDATES}"
    )


# Automatically determine bucket once
S3_BUCKET = get_working_bucket()


def list_s3_files(bucket: str = None, prefix: str = ""):
    bucket = bucket or S3_BUCKET

    try:
        paginator = s3.get_paginator("list_objects_v2")
        keys = []

        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])

        return keys

    except Exception as e:
        logging.error(f"Error listing S3 files: {e}")
        return []