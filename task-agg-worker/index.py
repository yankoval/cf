import os
import json
import logging
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def get_s3_client():
    endpoint_url = os.getenv('S3_ENDPOINT', 'https://storage.yandexcloud.net')
    # If S3_ENDPOINT is set to empty string, use None so boto3 uses its default.
    # This is helpful for testing with moto.
    if endpoint_url == '':
        endpoint_url = None

    return boto3.client(
        service_name='s3',
        endpoint_url=endpoint_url,
        aws_access_key_id=os.getenv('S3_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('S3_SECRET_ACCESS_KEY'),
        region_name=os.getenv('AWS_REGION', 'ru-central1'),
        config=Config(signature_version='s3v4')
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
            # Use 'utf-8-sig' to automatically handle Byte Order Mark (BOM) if present
            content_bytes = response['Body'].read()
            try:
                content = content_bytes.decode('utf-8-sig')
                data = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                logger.error(f"Failed to parse JSON for {object_id}: {str(e)}")
                s3.put_object_tagging(
                    Bucket=bucket_id,
                    Key=object_id,
                    Tagging={'TagSet': [{'Key': 'status', 'Value': 'error'}]}
                )
                continue

            # New logic to handle the new JSON structure
            pasport_data = data.get('PasportData', {})

            # 1. id is the filename without extension
            file_id = os.path.splitext(os.path.basename(object_id))[0]

            # 2. gtin normalization (ensure 14 digits)
            gtin = str(data.get('Gtin', ''))
            if len(gtin) == 13:
                gtin = '0' + gtin

            # 3. numРacksInBox from PasportData.Product_PackQty
            num_packs = pasport_data.get('Product_PackQty')

            # 4. boxLabelFields from PasportData.Product_name_part1
            label_fields = pasport_data.get('Product_name_part1')

            # Populate data with expected fields for downstream processing
            data['id'] = file_id
            data['gtin'] = gtin
            data['numРacksInBox'] = num_packs  # Note: Cyrillic 'Р' (U+0420)
            data['boxLabelFields'] = label_fields

            # Validate mandatory fields
            mandatory_fields = ['id', 'gtin', 'numРacksInBox', 'boxLabelFields']
            missing_fields = [f for f in mandatory_fields if data.get(f) is None or data.get(f) == '']
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
                        'Key': report_key
                    },
                    ExpiresIn=url_expiration
                )
            except ClientError as e:
                logger.error(f"Error generating presigned URL: {e}")
                continue

            # Update JSON data
            data['task-export-signed-link'] = presigned_url

            # Upload modified file
            dest_key = f"{dest_path_prefix}{file_id}-{data['gtin']}.json"
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
