"""Runnable, dependency-light tests for the sg_audit deterministic engines.

Pure-analyzer tests only (no boto3/DB/AWS) so they run anywhere:

    python -m sg_audit.tests.test_analyzers
"""
