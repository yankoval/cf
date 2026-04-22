import os
import json
import logging
import boto3

# Настройка логирования
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Глобальный клиент SQS для переиспользования между вызовами (warm starts)
sqs_client = None

def get_sqs_client():
    """Создает и возвращает клиент для работы с Yandex Message Queue (SQS)."""
    global sqs_client
    if sqs_client is None:
        sqs_client = boto3.client(
            service_name='sqs',
            endpoint_url=os.getenv('SQS_ENDPOINT', 'https://message-queue.api.cloud.yandex.net'),
            aws_access_key_id=os.getenv('S3_ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('S3_SECRET_ACCESS_KEY'),
            region_name=os.getenv('AWS_REGION', 'ru-central1')
        )
    return sqs_client

def handler(event, context):
    """
    Обработчик триггера Object Storage.
    Извлекает информацию о созданном/измененном объекте и отправляет сообщение в очередь.
    """
    try:
        sqs = get_sqs_client()
    except Exception as e:
        logger.error(f"Failed to initialize SQS client: {str(e)}")
        return {
            'statusCode': 500,
            'body': 'Failed to initialize SQS client'
        }

    queue_url = os.getenv('QUEUE_URL')

    if not queue_url:
        logger.error("QUEUE_URL environment variable is not set")
        return {
            'statusCode': 500,
            'body': 'QUEUE_URL environment variable is required'
        }

    messages = event.get('messages', [])
    processed_count = 0

    for message in messages:
        details = message.get('details', {})
        bucket_id = details.get('bucket_id')
        object_id = details.get('object_id')

        if not bucket_id or not object_id:
            logger.warning(f"Missing bucket_id or object_id in message: {json.dumps(message)}")
            continue

        # Формируем тело сообщения для воркера
        queue_message = {
            'bucket': bucket_id,
            'key': object_id
        }

        try:
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(queue_message)
            )
            logger.info(f"Message sent to queue for object: s3://{bucket_id}/{object_id}")
            processed_count += 1
        except Exception as e:
            logger.error(f"Failed to send message to queue for object {bucket_id}/{object_id}: {str(e)}")
            # Мы не выбрасываем исключение здесь, чтобы попытаться обработать остальные сообщения в батче,
            # но в реальной системе может потребоваться более сложная обработка ошибок.

    return {
        'statusCode': 200,
        'body': f"Processed {processed_count} messages"
    }
