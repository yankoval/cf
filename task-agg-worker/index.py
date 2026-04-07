import os
import json
import logging
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def get_s3_client():
    return boto3.client(
        service_name='s3',
        endpoint_url=os.getenv('S3_ENDPOINT', 'https://storage.yandexcloud.net'),
        aws_access_key_id=os.getenv('S3_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('S3_SECRET_ACCESS_KEY'),
        region_name='ru-central1'
    )

def handler(event, context):
    s3 = get_s3_client()
    bucket_env = os.getenv('BUCKET')
    filter_prefix = os.getenv('FILTER_PREFIX', '')
    report_path_prefix = os.getenv('REPORT_PATH_PREFIX', 'reports/').rstrip('/') + '/'
    dest_path_prefix = os.getenv('DEST_PATH_PREFIX', 'processed/').rstrip('/') + '/'
    url_expiration = int(os.getenv('URL_EXPIRATION', 3600))

    for message in event.get('messages', []):
        details = message.get('details', {})
        bucket_id = details.get('bucket_id')
        object_id = details.get('object_id')

        if not bucket_id or not object_id:
            continue

        # Check prefix
        if not object_id.startswith(filter_prefix):
            logger.info(f"Skipping {object_id}: does not match prefix {filter_prefix}")
            continue

        try:
            # Check existing tags to avoid double processing
            tagging = s3.get_object_tagging(Bucket=bucket_id, Key=object_id)
            tags = {t['Key']: t['Value'] for t in tagging.get('TagSet', [])}
            if 'status' in tags:
                logger.info(f"Skipping {object_id}: already has status {tags['status']}")
                continue

            # Check extension
            if not object_id.lower().endswith('.json'):
                logger.error(f"File {object_id} is not a JSON file.")
                s3.put_object_tagging(
                    Bucket=bucket_id,
                    Key=object_id,
                    Tagging={'TagSet': [{'Key': 'status', 'Value': 'error'}]}
                )
                continue

            # Get object content
            response = s3.get_object(Bucket=bucket_id, Key=object_id)
            content = response['Body'].read().decode('utf-8')
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                logger.error(f"Failed to parse JSON for {object_id}")
                s3.put_object_tagging(
                    Bucket=bucket_id,
                    Key=object_id,
                    Tagging={'TagSet': [{'Key': 'status', 'Value': 'error'}]}
                )
                continue

            # Validate mandatory fields
            # Note: numРacksInBox contains Cyrillic 'Р' (U+0420)
            mandatory_fields = ['id', 'gtin', 'numРacksInBox', 'boxLabelFields']
            missing_fields = [f for f in mandatory_fields if f not in data]
            if missing_fields:
                logger.error(f"Missing mandatory fields in {object_id}: {missing_fields}")
                s3.put_object_tagging(
                    Bucket=bucket_id,
                    Key=object_id,
                    Tagging={'TagSet': [{'Key': 'status', 'Value': 'error'}]}
                )
                continue

            # Generate presigned URL for report upload
            report_key = f"{report_path_prefix}{data['id']}.json"

            try:
                presigned_url = s3.generate_presigned_url(
                    'put_object',
                    Params={
                        'Bucket': bucket_env,
                        'Key': report_key,
                        'ContentType': 'application/json',
                        'ACL': 'public-read'
                    },
                    ExpiresIn=url_expiration
                )
            except ClientError as e:
                logger.error(f"Error generating presigned URL: {e}")
                continue

            # Update JSON data
            data['task-export-signed-link'] = presigned_url

            # Upload modified file
            dest_key = f"{dest_path_prefix}{data['gtin']}-{data['id']}.json"
            s3.put_object(
                Bucket=bucket_env,
                Key=dest_key,
                Body=json.dumps(data, ensure_ascii=False).encode('utf-8'),
                ContentType='application/json'
            )

            # Tag original file as processed
            s3.put_object_tagging(
                Bucket=bucket_id,
                Key=object_id,
                Tagging={'TagSet': [{'Key': 'status', 'Value': 'processed'}]}
            )
            logger.info(f"Successfully processed {object_id} -> {dest_key}")

        except Exception as e:
            logger.exception(f"Error processing {object_id}: {e}")
            try:
                s3.put_object_tagging(
                    Bucket=bucket_id,
                    Key=object_id,
                    Tagging={'TagSet': [{'Key': 'status', 'Value': 'error'}]}
                )
            except:
                pass

    return {
        'statusCode': 200,
        'body': 'OK'
    }
