import pymysql
import boto3
import json
import os
from dotenv import load_dotenv
from botocore.exceptions import ClientError
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool
from dbutils.pooled_db import PooledDB

load_dotenv()
rds_host = "bytoiddb.c9ek8228ux41.ca-central-1.rds.amazonaws.com"


def get_secret():
    secret_name = "rds!db-9db402d8-3595-4048-bf23-979d5e5985e4"
    region_name = "ca-central-1"

    client = boto3.client("secretsmanager", region_name=region_name)
    response = client.get_secret_value(SecretId=secret_name)

    if "SecretString" in response:
        return json.loads(response["SecretString"])
    else:
        import base64

        return json.loads(base64.b64decode(response["SecretBinary"]))


creds = get_secret()
# engine = create_engine(
#     f"mysql+pymysql://{creds['username']}:{creds['password']}@{rds_host}:3306/bytoid_support_agent",
#     poolclass=QueuePool,
#     pool_size=100,  # number of persistent connections
#     max_overflow=5,  # extra connections allowed temporarily
#     pool_recycle=3600,  # recycle connections after 1h
#     pool_pre_ping=True,  # check before using a connection
# )

pool = PooledDB(
    creator=pymysql,
    maxconnections=120,
    mincached=10,
    blocking=True,
    host=rds_host,
    user=creds["username"],
    password=creds["password"],
    database="bytoid_support_agent",
    port=3306,
    charset="utf8mb4",
)


def connect_to_rds():
    # creds = get_secret()
    try:
        # connection = pymysql.connect(
        #     host=rds_host,
        #     user=creds["username"],
        #     password=creds["password"],
        #     db="bytoid_support_agent",
        #     port=3306,
        #     connect_timeout=10,
        # )
        # # print("\u2705 Connection successful!")
        # return connection
        return pool.connection()
    except pymysql.MySQLError as e:
        print("\u274c Error connecting to RDS:", e)
        return None


def start_rds_instance():
    rds = boto3.client(
        "rds",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name="ca-central-1",
    )
    response = rds.describe_db_instances()
    for db in response["DBInstances"]:
        print(
            f"Instance ID: {db['DBInstanceIdentifier']} | Endpoint: {db['Endpoint']['Address']}"
        )


from contextlib import contextmanager


# Context manager for safe cursor usage


@contextmanager
def get_cursor(conn):
    if conn is None:
        raise ConnectionError("No RDS connection available.")
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


# start_rds_instance()
# connect_to_rds()
# get_secret()
