import os
import sys
import json
import time
import boto3
import requests
import logging
import io
from PIL import Image, ImageDraw, ImageFont

# Настройка логирования
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Игнорируем прокси в среде Yandex Cloud для корректной работы библиотек
os.environ['no_proxy'] = '*'

# Глобальный клиент S3 для переиспользования
s3_client = None

def get_s3_client():
    """Инициализация клиента S3 для Yandex Cloud."""
    global s3_client
    if s3_client is None:
        s3_client = boto3.client(
            service_name='s3',
            endpoint_url=os.environ.get('S3_ENDPOINT', 'https://storage.yandexcloud.net'),
            region_name='ru-central1',
            aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY')
        )
    return s3_client

def create_info_image(report_data):
    """
    Генерирует PNG изображение с информацией из отчета.
    """
    text = (
        f"ID: {report_data['id']}\n"
        f"Оператор: {report_data['operator']}\n"
        f"Коробок: {report_data['boxes']}\n"
        f"Продуктов: {report_data['products']}"
    )

    # Попытка найти шрифт, поддерживающий кириллицу
    font = None
    # Сначала ищем в текущей директории функции
    local_font_path = os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf")
    font_paths = [
        local_font_path,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/ttf-dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
    ]

    font_size = 20
    for path in font_paths:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, font_size)
                break
            except Exception:
                continue

    if font is None:
        logger.warning("Could not find a TrueType font, falling back to default (Cyrillic may not be supported)")
        font = ImageFont.load_default()

    # Создаем временное изображение для расчета размеров
    dummy_img = Image.new('RGB', (1, 1))
    draw = ImageDraw.Draw(dummy_img)

    # Получаем размеры текста
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
        # Для старых версий Pillow
        tw, th = draw.textsize(text, font=font)

    padding = 30
    border_width = 3
    width = tw + padding * 2
    height = th + padding * 2

    # Создаем итоговое изображение
    img = Image.new('RGB', (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    # Рисуем рамку
    draw.rectangle([0, 0, width-1, height-1], outline=(180, 180, 180), width=border_width)

    # Рисуем текст
    draw.text((padding, padding), text, font=font, fill=(40, 40, 40))

    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format='PNG')
    return img_byte_arr.getvalue()

def handler(event, context):
    """
    Обработчик события Object Storage.
    Анализирует JSON, создает PNG и отправляет в MAX API.
    """
    TOKEN = os.environ.get("MAX_BOT_TOKEN")
    CHAT_ID = os.environ.get("MAX_CHAT_ID")
    MAX_API_URL = "https://platform-api.max.ru/messages"
    UPLOAD_INIT_URL = "https://platform-api.max.ru/uploads"

    if not TOKEN or not CHAT_ID:
        logger.error("Missing MAX_BOT_TOKEN or MAX_CHAT_ID environment variables")
        return {'statusCode': 500, 'body': 'Configuration error'}

    s3 = get_s3_client()
    headers = {"Authorization": TOKEN}

    for message in event.get('messages', []):
        details = message.get('details', {})
        bucket_id = details.get('bucket_id')
        object_id = details.get('object_id')

        if not bucket_id or not object_id:
            continue

        try:
            logger.info(f"Processing file {object_id} from bucket {bucket_id}")

            # --- ШАГ 0: ЗАГРУЗКА И ПАРСИНГ ---
            s3_response = s3.get_object(Bucket=bucket_id, Key=object_id)
            content = s3_response['Body'].read().decode('utf-8-sig')
            data = json.loads(content)

            report_id = data.get('id', 'N/A')
            operator = data.get('operator', 'N/A')
            ready_boxes = data.get('readyBox', [])

            boxes_count = len(ready_boxes)
            products_count = sum(len(box.get('productNumbersFull', [])) for box in ready_boxes)

            report_info = {
                'id': report_id,
                'operator': operator,
                'boxes': boxes_count,
                'products': products_count
            }

            message_text = (
                f"Отчёт оборудования\n"
                f"ID: {report_id}\n"
                f"Оператор: {operator}\n"
                f"Количество коробок: {boxes_count}\n"
                f"Количество продуктов: {products_count}"
            )

            # --- ШАГ 1: ГЕНЕРАЦИЯ PNG ---
            logger.info("Generating PNG image")
            png_bytes = create_info_image(report_info)
            file_name = f"report_{report_id}.png"

            # --- ШАГ 2: ПОЛУЧЕНИЕ URL ДЛЯ ЗАГРУЗКИ ---
            logger.info("Requesting upload URL from MAX API")
            res_init = requests.post(
                UPLOAD_INIT_URL,
                params={"type": "file"},
                headers=headers,
                timeout=10
            )
            res_init.raise_for_status()
            upload_url = res_init.json().get('url')

            if not upload_url:
                logger.error(f"Server did not return upload URL. Response: {res_init.text}")
                continue

            # --- ШАГ 3: ЗАГРУЗКА PNG ---
            logger.info(f"Uploading PNG to {upload_url}")
            files = {'file': (file_name, png_bytes, 'image/png')}
            res_upload = requests.post(upload_url, headers=headers, files=files, timeout=30)
            res_upload.raise_for_status()

            file_token = res_upload.json().get('token')
            if not file_token:
                logger.error("No token received after upload")
                continue

            # --- ШАГ 4: ОТПРАВКА СООБЩЕНИЯ С PNG ---
            logger.info(f"Sending message to chat {CHAT_ID}")

            payload = {
                "text": message_text,
                "attachments": [
                    {
                        "type": "file",
                        "payload": {"token": file_token}
                    }
                ],
                "notify": True
            }

            msg_headers = {
                "Authorization": TOKEN,
                "Content-Type": "application/json"
            }

            # Цикл ожидания готовности вложения (Polling)
            max_retries = 10
            success = False

            for i in range(max_retries):
                res_final = requests.post(
                    MAX_API_URL,
                    params={"chat_id": CHAT_ID},
                    json=payload,
                    headers=msg_headers,
                    timeout=20
                )

                if res_final.status_code == 200:
                    logger.info(f"SUCCESS: Message sent on attempt {i+1}")
                    success = True
                    break

                resp_json = res_final.json()
                if resp_json.get("code") == "attachment.not.ready":
                    wait_time = (i + 1) * 2
                    logger.info(f"Attachment not ready. Waiting {wait_time}s... (Attempt {i+1}/{max_retries})")
                    time.sleep(wait_time)
                else:
                    logger.error(f"API Error: {res_final.status_code} - {res_final.text}")
                    break

            if not success:
                logger.error(f"Failed to send message after {max_retries} attempts")

        except Exception as e:
            logger.error(f"Critical error processing object {object_id}: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Server response details: {e.response.text}")

    return {'statusCode': 200, 'body': 'OK'}
