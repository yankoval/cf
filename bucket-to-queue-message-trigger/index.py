import os
import json
import logging
import boto3
import uuid
import base64

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
    Формирует сообщение в формате Celery v2 и отправляет его в очередь.
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
    task_name = os.getenv('CELERY_TASK_NAME', 'tasks.process_s3_event')
    routing_key = os.getenv('CELERY_ROUTING_KEY', 'queue_task_create_1C')

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

        # Данные для задачи
        task_args = [{"bucket": bucket_id, "key": object_id}]
        task_kwargs = {}
        task_id = str(uuid.uuid4())

        # Формируем тело сообщения согласно протоколу Celery v2
        # Тело сообщения (body) должно быть сериализовано в JSON, а затем закодировано в Base64
        body_data = (task_args, task_kwargs, {"callbacks": None, "errbacks": None, "chain": None, "chord": None})
        body_json = json.dumps(body_data)
        body_b64 = base64.b64encode(body_json.encode('utf-8')).decode('utf-8')

        # Конверт Celery
        celery_message = {
            "body": body_b64,
            "content-encoding": "utf-8",
            "content-type": "application/json",
            "headers": {
                "lang": "py",
                "task": task_name,
                "id": task_id,
                "shadow": None,
                "eta": None,
                "expires": None,
                "group": None,
                "group_index": None,
                "retries": 0,
                "timelimit": [None, None],
                "root_id": task_id,
                "parent_id": None,
                "argsrepr": repr(task_args),
                "kwargsrepr": repr(task_kwargs),
                "origin": "gen@" + os.getenv('HOSTNAME', 'yandex-cloud-function')
            },
            "properties": {
                "correlation_id": task_id,
                "reply_to": "",
                "delivery_mode": 2,
                "delivery_info": {
                    "exchange": "",
                    "routing_key": routing_key
                },
                "priority": 0,
                "body_encoding": "base64",
                "delivery_tag": str(uuid.uuid4())
            }
        }

        try:
            sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(celery_message)
            )
            logger.info(f"Celery message sent to queue for object: s3://{bucket_id}/{object_id}, Task ID: {task_id}")
            processed_count += 1
        except Exception as e:
            logger.error(f"Failed to send Celery message to queue for object {bucket_id}/{object_id}: {str(e)}")

    return {
        'statusCode': 200,
        'body': f"Processed {processed_count} messages"
    }
