"""Idempotent boto3 deploy for the VRA OSINT collector Lambda.

Packages the collector (handler + the dependency-light ``vra`` modules it imports
+ pinned deps) into a zip and create-or-updates the function in ``AWS_REGION``
(default ca-central-1, matching RDS/KMS). Creates a least-privilege execution
role (CloudWatch Logs only — no KMS, the Lambda holds no keys).

This is operator tooling — run deliberately with an admin/CI credential that has
``iam:CreateRole`` + ``lambda:CreateFunction`` (NOT the app's scoped runtime
role). The app never invokes this.

    python -m vra.collector_lambda.deploy \
        --function vra-osint-collector \
        --callback-url https://api.bytoid.ai/vra/osint/callback \
        --hmac-secret "$VRA_HMAC_SECRET" \
        [--profile my-admin-profile] [--region ca-central-1]
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

HANDLER = "vra.collector_lambda.handler.lambda_handler"
RUNTIME = "python3.12"
TIMEOUT = 300
MEMORY = 512

# Minimal set of repo files the handler needs (all stdlib/requests-only).
_PACKAGE_FILES = [
    "vra/__init__.py",
    "vra/config.py",
    "vra/schema.py",
    "vra/osint",  # whole osint subpackage (safe_fetch, normalize, signing, collectors)
    "vra/collector_lambda/__init__.py",
    "vra/collector_lambda/handler.py",
]


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


def _build_zip() -> bytes:
    root = _repo_root()
    with tempfile.TemporaryDirectory() as build:
        # 1) install deps into the bundle root
        req = os.path.join(root, "vra", "collector_lambda", "requirements.txt")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", req, "-t", build, "--quiet"]
        )
        # 2) copy the needed repo files preserving package paths
        for rel in _PACKAGE_FILES:
            src = os.path.join(root, rel)
            dst = os.path.join(build, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        # 3) zip it
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for base, _dirs, files in os.walk(build):
                for f in files:
                    if f.endswith(".pyc"):
                        continue
                    full = os.path.join(base, f)
                    zf.write(full, os.path.relpath(full, build))
        return buf.getvalue()


def _ensure_role(session, function: str) -> str:
    import json

    iam = session.client("iam")
    role_name = f"{function}-role"
    assume = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
    except iam.exceptions.NoSuchEntityException:
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume),
            Description="VRA OSINT collector — CloudWatch Logs only.",
        )["Role"]
        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
        print(f"Created role {role_name}; waiting for propagation…")
        time.sleep(12)
    return role["Arn"]


def deploy(args) -> None:
    import boto3

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    lam = session.client("lambda")
    role_arn = _ensure_role(session, args.function)
    code = _build_zip()
    env = {
        "Variables": {
            "AWS_REGION_HINT": args.region,
            "VRA_HMAC_SECRET": args.hmac_secret,
        }
    }

    try:
        lam.get_function(FunctionName=args.function)
        exists = True
    except lam.exceptions.ResourceNotFoundException:
        exists = False

    if exists:
        lam.update_function_code(FunctionName=args.function, ZipFile=code, Publish=True)
        lam.get_waiter("function_updated").wait(FunctionName=args.function)
        lam.update_function_configuration(
            FunctionName=args.function,
            Handler=HANDLER,
            Runtime=RUNTIME,
            Timeout=TIMEOUT,
            MemorySize=MEMORY,
            Environment=env,
        )
        print(f"Updated {args.function} in {args.region}.")
    else:
        lam.create_function(
            FunctionName=args.function,
            Runtime=RUNTIME,
            Role=role_arn,
            Handler=HANDLER,
            Code={"ZipFile": code},
            Timeout=TIMEOUT,
            MemorySize=MEMORY,
            Environment=env,
            Publish=True,
        )
        print(f"Created {args.function} in {args.region}.")

    print(
        "\nNext: set VRA_LAMBDA_ARN + VRA_CALLBACK_BASE_URL + VRA_HMAC_SECRET in the "
        "app environment to enable collection.\n"
        f"  callback-url used: {args.callback_url}"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Deploy the VRA OSINT collector Lambda.")
    p.add_argument("--function", default="vra-osint-collector")
    p.add_argument("--region", default=os.getenv("AWS_REGION", "ca-central-1"))
    p.add_argument("--profile", default=None, help="admin/CI AWS profile (NOT the app role)")
    p.add_argument("--callback-url", required=True)
    p.add_argument("--hmac-secret", required=True)
    deploy(p.parse_args())


if __name__ == "__main__":
    main()
