import json
import boto3
import os
from ingestion.fetch_data import collect_all

s3 = boto3.client("s3")
BUCKET = os.getenv("BUCKET_NAME")

def lambda_handler(event, context):
    data = collect_all()

    key = f"raw/{data['timestamp']}.json"

    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(data),
        ContentType="application/json"
    )

    return {
        "statusCode": 200,
        "body": json.dumps({"message": "Data stored", "key": key})
    }