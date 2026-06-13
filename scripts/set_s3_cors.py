#!/usr/bin/env python3
"""Apply a CORS configuration to an S3 bucket so the browser SPA can PUT
presigned uploads and GET/HEAD evidence files directly.

Why this exists
---------------
The Intake Workflow "Upload Files to Auto-fill" flow hands the browser a
presigned ``put_object`` URL (see ``/make_s3upload``) and the browser PUTs the
file straight to S3. A PUT carrying ``Content-Type: application/octet-stream``
is NOT a CORS "simple request", so the browser fires a preflight ``OPTIONS``
first. If the bucket has no CORS configuration, that preflight is rejected and
the upload fails with ``TypeError: Failed to fetch`` — exactly the
``[FileUpload] Error`` seen on demo.bytoid.ai. The same gap blocks Drive-import
read-back and ``FilePreview`` HEAD/GET.

The allowed origins mirror ``utils.app_configs`` (all environments) so one CORS
document is valid for dev, staging and prod frontends.

Usage
-----
    # uses S3_BUCKET / S3_REGION from the environment (same as the app)
    python scripts/set_s3_cors.py

    # target a specific bucket and preview without writing
    python scripts/set_s3_cors.py --bucket bytoiddev --dry-run

Run it once per bucket (dev: ``bytoiddev``, plus the prod bucket).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make the repo root importable when run as ``python scripts/set_s3_cors.py``.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The CORS document is defined once in utils.s3_utils so the app's startup
# self-heal (utils.s3_utils.ensure_bucket_cors) and this manual script can
# never drift apart.
from utils.s3_utils import build_cors_config, s3bucket


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply CORS config to an S3 bucket.")
    parser.add_argument(
        "--bucket",
        default=os.getenv("S3_BUCKET"),
        help="Target bucket (defaults to the S3_BUCKET env var).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the CORS document without applying it.",
    )
    args = parser.parse_args()

    if not args.bucket:
        print("ERROR: no bucket given and S3_BUCKET is not set.", file=sys.stderr)
        return 2

    config = build_cors_config()
    print(f"Bucket: {args.bucket}")
    print("CORS configuration:")
    print(json.dumps(config, indent=2))

    if args.dry_run:
        print("\n--dry-run: not applied.")
        return 0

    s3 = s3bucket()
    s3.put_bucket_cors(Bucket=args.bucket, CORSConfiguration=config)
    print(f"\nApplied CORS to {args.bucket}.")

    # Read it back so the operator can confirm what S3 stored.
    applied = s3.get_bucket_cors(Bucket=args.bucket)
    print("Bucket now reports:")
    print(json.dumps(applied.get("CORSRules", []), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
