import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any

import boto3
import psycopg
from psycopg.types.json import Jsonb


DEFAULT_PREFIX = "raw/"
RAW_SCHEMA = "raw"
RAW_TABLE = "s3_news_payloads"
LOG_TABLE = "s3_ingestion_logs"


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _parse_payload_timestamp(payload: dict[str, Any]) -> datetime | None:
    raw_timestamp = payload.get("timestamp")
    if not raw_timestamp:
        return None

    try:
        parsed = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _ensure_sslmode(conninfo: str) -> str:
    if "sslmode=" in conninfo:
        return conninfo

    separator = "&" if "?" in conninfo else "?"
    return f"{conninfo}{separator}sslmode=require"


def ensure_raw_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"create schema if not exists {RAW_SCHEMA}")
        cur.execute(
            f"""
            create table if not exists {RAW_SCHEMA}.{RAW_TABLE} (
                id bigserial primary key,
                s3_bucket text not null,
                s3_key text not null,
                s3_last_modified timestamptz,
                s3_etag text,
                payload jsonb not null,
                payload_timestamp timestamptz,
                ingested_at timestamptz not null default now(),
                unique (s3_bucket, s3_key, s3_etag)
            )
            """
        )
    conn.commit()


def ensure_log_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"create schema if not exists {RAW_SCHEMA}")
        cur.execute(
            f"""
            create table if not exists {RAW_SCHEMA}.{LOG_TABLE} (
                id bigserial primary key,
                pipeline_name text not null,
                source text not null,
                s3_bucket text,
                s3_prefix text,
                status text not null,
                started_at timestamptz not null default now(),
                finished_at timestamptz,
                inserted_count integer not null default 0,
                skipped_count integer not null default 0,
                failed_count integer not null default 0,
                error_message text,
                metadata jsonb not null default '{{}}'::jsonb
            )
            """
        )
    conn.commit()


def ensure_tables(conn: psycopg.Connection) -> None:
    ensure_raw_table(conn)
    ensure_log_table(conn)


def start_ingestion_log(
    conn: psycopg.Connection,
    *,
    bucket: str,
    prefix: str,
    limit: int | None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            insert into {RAW_SCHEMA}.{LOG_TABLE} (
                pipeline_name,
                source,
                s3_bucket,
                s3_prefix,
                status,
                metadata
            )
            values (%s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                "s3_to_neon_raw_ingestion",
                "s3",
                bucket,
                prefix,
                "running",
                Jsonb({"limit": limit}),
            ),
        )
        log_id = cur.fetchone()[0]
    conn.commit()
    return log_id


def finish_ingestion_log(
    conn: psycopg.Connection,
    *,
    log_id: int,
    status: str,
    inserted: int,
    skipped: int,
    failed: int,
    error_message: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            update {RAW_SCHEMA}.{LOG_TABLE}
            set
                status = %s,
                finished_at = now(),
                inserted_count = %s,
                skipped_count = %s,
                failed_count = %s,
                error_message = %s
            where id = %s
            """,
            (status, inserted, skipped, failed, error_message, log_id),
        )
    conn.commit()


def iter_s3_objects(bucket: str, prefix: str = DEFAULT_PREFIX, limit: int | None = None):
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")

    seen = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue

            yield {
                "bucket": bucket,
                "key": key,
                "last_modified": obj.get("LastModified"),
                "etag": obj.get("ETag", "").strip('"'),
            }

            seen += 1
            if limit is not None and seen >= limit:
                return


def load_json_from_s3(bucket: str, key: str) -> dict[str, Any]:
    s3 = boto3.client("s3")
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read().decode("utf-8")
    payload = json.loads(body)

    if not isinstance(payload, dict):
        raise ValueError(f"S3 object must contain a JSON object: s3://{bucket}/{key}")

    return payload


def insert_payload(
    conn: psycopg.Connection,
    *,
    bucket: str,
    key: str,
    last_modified: datetime | None,
    etag: str | None,
    payload: dict[str, Any],
) -> bool:
    payload_timestamp = _parse_payload_timestamp(payload)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            insert into {RAW_SCHEMA}.{RAW_TABLE} (
                s3_bucket,
                s3_key,
                s3_last_modified,
                s3_etag,
                payload,
                payload_timestamp
            )
            values (%s, %s, %s, %s, %s, %s)
            on conflict (s3_bucket, s3_key, s3_etag) do nothing
            """,
            (
                bucket,
                key,
                last_modified,
                etag,
                Jsonb(payload),
                payload_timestamp,
            ),
        )
        inserted = cur.rowcount == 1

    return inserted


def ingest_s3_raw_to_neon(
    *,
    bucket: str,
    database_url: str,
    prefix: str = DEFAULT_PREFIX,
    limit: int | None = None,
) -> dict[str, int]:
    inserted = 0
    skipped = 0
    failed = 0

    with psycopg.connect(_ensure_sslmode(database_url)) as conn:
        ensure_tables(conn)
        log_id = start_ingestion_log(conn, bucket=bucket, prefix=prefix, limit=limit)

        try:
            for obj in iter_s3_objects(bucket=bucket, prefix=prefix, limit=limit):
                payload = load_json_from_s3(bucket=obj["bucket"], key=obj["key"])
                was_inserted = insert_payload(
                    conn,
                    bucket=obj["bucket"],
                    key=obj["key"],
                    last_modified=obj["last_modified"],
                    etag=obj["etag"],
                    payload=payload,
                )
                conn.commit()

                if was_inserted:
                    inserted += 1
                else:
                    skipped += 1
        except Exception as exc:
            conn.rollback()
            failed += 1
            finish_ingestion_log(
                conn,
                log_id=log_id,
                status="failed",
                inserted=inserted,
                skipped=skipped,
                failed=failed,
                error_message=str(exc),
            )
            raise

        finish_ingestion_log(
            conn,
            log_id=log_id,
            status="success",
            inserted=inserted,
            skipped=skipped,
            failed=failed,
        )

    return {"inserted": inserted, "skipped": skipped, "failed": failed}


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest raw JSON files from S3 into Neon.")
    parser.add_argument("--bucket", default=os.getenv("S3_BUCKET") or os.getenv("BUCKET_NAME"))
    parser.add_argument("--prefix", default=os.getenv("S3_PREFIX", DEFAULT_PREFIX))
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    bucket = args.bucket or _required_env("S3_BUCKET")
    database_url = args.database_url or _required_env("DATABASE_URL")

    result = ingest_s3_raw_to_neon(
        bucket=bucket,
        database_url=database_url,
        prefix=args.prefix,
        limit=args.limit,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
