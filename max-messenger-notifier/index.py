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
    Генерирует PNG изображение с информацией из отчета, оптимизированное для портретного режима телефона.
    """
    lines = [
        ("ОТЧЁТ ОБОРУДОВАНИЯ", True),
        (f"ID: {report_data['id']}", False),
        (f"Оператор: {report_data['operator']}", False),
        (f"Коробок: {report_data['boxes']}", False),
        (f"Продуктов: {report_data['products']}", False)
    ]

    # Попытка найти шрифт
    font_path = None
    local_font_path = os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf")
    check_paths = [
        local_font_path,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/ttf-dejavu/DejaVuSans.ttf"
    ]
    for path in check_paths:
        if os.path.exists(path):
            font_path = path
            break

    # Настройки для "карточки"
    width = 600 # Фиксированная ширина для предсказуемости на мобильных
    padding = 40
    line_spacing = 15
    title_font_size = 32
    body_font_size = 24

    if font_path:
        title_font = ImageFont.truetype(font_path, title_font_size)
        body_font = ImageFont.truetype(font_path, body_font_size)
    else:
        title_font = body_font = ImageFont.load_default()

    # Сначала считаем высоту
    total_height = padding * 2
    draw_test = ImageDraw.Draw(Image.new('RGB', (1, 1)))

    prepared_lines = []
    for text, is_title in lines:
        f = title_font if is_title else body_font
        # Разбивка длинного ID по словам или символам не нужна, если мы просто хотим портрет,
        # но ID может быть очень длинным.

        # Функция для переноса длинных строк (например, ID)
        def wrap_text(t, font, max_w):
            words = []
            # Для ID пробуем разбить по дефисам
            if '-' in t and len(t) > 20:
                parts = t.split('-')
                current_line = parts[0]
                for part in parts[1:]:
                    test_line = current_line + '-' + part
                    bbox = draw_test.textbbox((0, 0), test_line, font=font)
                    if bbox[2] - bbox[0] <= max_w:
                        current_line = test_line
                    else:
                        words.append(current_line + '-')
                        current_line = part
                words.append(current_line)
            else:
                words = [t]
            return words

        wrapped = wrap_text(text, f, width - padding * 2)
        for w_line in wrapped:
            bbox = draw_test.textbbox((0, 0), w_line, font=f)
            h = bbox[3] - bbox[1]
            prepared_lines.append((w_line, f, h))
            total_height += h + line_spacing

    total_height -= line_spacing # Убираем лишний отступ в конце

    # Создаем итоговое изображение
    img = Image.new('RGB', (width, total_height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    # Рисуем рамку
    draw.rectangle([0, 0, width-1, total_height-1], outline=(200, 200, 200), width=4)

    # Рисуем текст
    current_y = padding
    for text, font, h in prepared_lines:
        draw.text((padding, current_y), text, font=font, fill=(30, 30, 30))
        current_y += h + line_spacing

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
