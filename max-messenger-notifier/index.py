import io
import json
import logging
import os
import re
import time
from collections.abc import Iterable, Mapping, Sequence

import boto3
import requests
from barcode import get_barcode_class
from barcode.writer import ImageWriter
from botocore.exceptions import ClientError
from PIL import Image, ImageDraw, ImageFont


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# В среде Yandex Cloud обращения к Object Storage и MAX не должны идти через
# автоматически подставленный proxy.
os.environ["no_proxy"] = "*"

MAX_API_URL = "https://platform-api.max.ru/messages"
MAX_UPLOAD_URL = "https://platform-api.max.ru/uploads"
DEFAULT_TASKS_PREFIX = "equipment-tasks"

s3_client = None


def normalize_sscc(value: object, field_name: str = "SSCC") -> str:
    """Вернуть SSCC без AI 00 в виде 18 цифр."""
    code = str(value).strip()
    if len(code) == 20 and code.startswith("00"):
        code = code[2:]
    if len(code) != 18 or not code.isdigit():
        raise ValueError(
            f"{field_name}: ожидался 18-значный SSCC, получено {value!r}"
        )

    payload = code[:-1]
    expected_check_digit = (
        10
        - sum(
            int(digit) * (3 if index % 2 == 0 else 1)
            for index, digit in enumerate(reversed(payload))
        )
        % 10
    ) % 10
    if int(code[-1]) != expected_check_digit:
        raise ValueError(
            f"{field_name}: неверная контрольная цифра SSCC {code}"
        )
    return code


def pallet_gs1_value(pallet_number: object) -> str:
    """Вернуть данные GS1-128: AI 00 и 18-значный SSCC."""
    return f"00{normalize_sscc(pallet_number, 'palletNumber')}"


def compact_ssccs(
    ssccs: Iterable[str],
    *,
    prefix_digits: int = 1,
    company_id_digits: int = 9,
) -> str:
    """Вернуть компактный список серийных номеров коробов."""
    numbers: set[str] = set()

    for original_code in ssccs:
        code = normalize_sscc(original_code)
        serial_start = prefix_digits + company_id_digits
        box_number = code[serial_start:-1]
        if not box_number:
            raise ValueError(
                f"Не удалось выделить номер короба: {original_code!r}"
            )
        numbers.add(box_number)

    if not numbers:
        return ""

    ordered = sorted(numbers, key=int)
    ranges: list[str] = []
    start = previous = ordered[0]

    for current in ordered[1:]:
        if int(current) == int(previous) + 1:
            previous = current
            continue
        ranges.append(start if start == previous else f"{start}-{previous}")
        start = previous = current

    ranges.append(start if start == previous else f"{start}-{previous}")
    return ", ".join(ranges)


def get_report_pallets(report: Mapping) -> tuple[Mapping, ...]:
    """Вернуть паллеты v2 и отклонить неоднозначный формат."""
    schema_version = report.get("schemaVersion")
    if schema_version == 2:
        raw_pallets = report.get("readyPallet")
        if not isinstance(raw_pallets, Sequence) or isinstance(
            raw_pallets, (str, bytes)
        ):
            raise ValueError("readyPallet должен быть массивом")
        if not raw_pallets:
            raise ValueError("readyPallet не должен быть пустым")

        pallets = []
        for pallet_index, pallet in enumerate(raw_pallets):
            if not isinstance(pallet, Mapping):
                raise ValueError(
                    f"readyPallet[{pallet_index}] должен быть объектом"
                )
            pallets.append(pallet)
        return tuple(pallets)

    if "readyPallet" in report:
        raise ValueError(
            "Отчёт с readyPallet должен содержать schemaVersion=2"
        )
    if schema_version not in (None, 1):
        raise ValueError(
            f"Неподдерживаемая версия отчёта: {schema_version!r}"
        )
    return ()


def get_report_boxes(report: Mapping) -> tuple[Mapping, ...]:
    """Вернуть физические короба из v1 или из всех паллетов v2."""
    pallets = get_report_pallets(report)
    locations_and_boxes = []

    if pallets:
        for pallet_index, pallet in enumerate(pallets):
            raw_boxes = pallet.get("readyBox")
            if not isinstance(raw_boxes, Sequence) or isinstance(
                raw_boxes, (str, bytes)
            ):
                raise ValueError(
                    f"readyPallet[{pallet_index}].readyBox должен быть массивом"
                )
            if not raw_boxes:
                raise ValueError(
                    f"readyPallet[{pallet_index}].readyBox не должен быть пустым"
                )
            locations_and_boxes.extend(
                (
                    f"readyPallet[{pallet_index}].readyBox[{box_index}]",
                    box,
                )
                for box_index, box in enumerate(raw_boxes)
            )
    else:
        raw_boxes = report.get("readyBox", [])
        if not isinstance(raw_boxes, Sequence) or isinstance(
            raw_boxes, (str, bytes)
        ):
            raise ValueError("readyBox должен быть массивом")
        locations_and_boxes.extend(
            (f"readyBox[{box_index}]", box)
            for box_index, box in enumerate(raw_boxes)
        )

    boxes = []
    for location, box in locations_and_boxes:
        if not isinstance(box, Mapping):
            raise ValueError(f"{location} должен быть объектом")
        boxes.append(box)
    return tuple(boxes)


def compact_box_numbers(boxes: Iterable[Mapping]) -> str:
    """Сформировать компактный перечень SSCC переданных коробов."""
    try:
        return compact_ssccs(
            box["boxNumber"]
            for box in boxes
            if box.get("boxNumber")
        )
    except ValueError:
        return "—"


def compact_report_boxes(report: Mapping) -> str:
    """Сформировать компактный перечень коробов отчёта v1/v2."""
    return compact_box_numbers(get_report_boxes(report))


def extract_report_info(report: Mapping) -> dict:
    """Проверить отчёт и собрать данные для уведомления."""
    if not isinstance(report, Mapping):
        raise ValueError("Отчёт должен быть JSON-объектом")

    pallets = get_report_pallets(report)
    boxes = get_report_boxes(report)
    pallet_numbers = []
    pallet_details = []
    product_counts = []

    for box_index, box in enumerate(boxes):
        products = box.get("productNumbersFull", [])
        if not isinstance(products, Sequence) or isinstance(
            products, (str, bytes)
        ):
            raise ValueError(
                f"Короб {box_index}: productNumbersFull должен быть массивом"
            )
        product_counts.append(len(products))

    box_offset = 0
    for pallet_index, pallet in enumerate(pallets):
        if pallet.get("palletAggregate") is not True:
            raise ValueError(
                f"readyPallet[{pallet_index}].palletAggregate должен быть true"
            )
        pallet_number = normalize_sscc(
            pallet.get("palletNumber"),
            f"readyPallet[{pallet_index}].palletNumber",
        )
        pallet_numbers.append(pallet_number)

        pallet_boxes = pallet.get("readyBox", [])
        for box_index, box in enumerate(pallet_boxes):
            location = (
                f"readyPallet[{pallet_index}].readyBox[{box_index}]"
            )
            if box.get("boxAgregate") is not True:
                raise ValueError(
                    f"{location}.boxAgregate должен быть true"
                )
            normalize_sscc(
                box.get("boxNumber"),
                f"{location}.boxNumber",
            )

        next_box_offset = box_offset + len(pallet_boxes)
        pallet_products = sum(
            product_counts[box_offset:next_box_offset]
        )
        box_offset = next_box_offset
        pallet_details.append(
            {
                "pallet_number": pallet_number,
                "boxes": len(pallet_boxes),
                "products": pallet_products,
                "ssccs": compact_box_numbers(pallet_boxes),
            }
        )

    return {
        "id": str(report.get("id") or "N/A"),
        "operator": str(report.get("operator") or "N/A"),
        "schema_version": 2 if pallets else 1,
        "pallets": len(pallets),
        "pallet_numbers": pallet_numbers,
        "pallet_details": pallet_details,
        "boxes": len(boxes),
        "products": sum(product_counts),
        "ssccs": compact_report_boxes(report),
    }


def get_s3_client():
    """Инициализация клиента S3 для Yandex Cloud."""
    global s3_client
    if s3_client is None:
        s3_client = boto3.client(
            service_name="s3",
            endpoint_url=os.environ.get(
                "S3_ENDPOINT",
                "https://storage.yandexcloud.net",
            ),
            region_name="ru-central1",
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )
    return s3_client


def _read_s3_json(s3, bucket_id: str, object_id: str) -> dict:
    response = s3.get_object(Bucket=bucket_id, Key=object_id)
    content = response["Body"].read().decode("utf-8-sig")
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError(f"{object_id}: ожидался JSON-объект")
    return data


def _read_optional_s3_json(s3, bucket_id: str, object_id: str) -> dict | None:
    """Прочитать JSON, вернув None только если объект не существует."""
    try:
        return _read_s3_json(s3, bucket_id, object_id)
    except ClientError as error:
        response = error.response or {}
        error_data = response.get("Error", {})
        metadata = response.get("ResponseMetadata", {})
        if (
            error_data.get("Code") in {"NoSuchKey", "NotFound", "404"}
            or metadata.get("HTTPStatusCode") == 404
        ):
            return None
        raise


def validate_report_identity(
    object_id: str,
    report_info: Mapping,
) -> None:
    """Для v2 связать имя объекта с ID отчёта до чтения задания."""
    if report_info["schema_version"] != 2:
        return

    file_name = object_id.rsplit("/", 1)[-1]
    if not file_name.endswith(".json"):
        raise ValueError(
            f"{object_id}: имя v2-отчёта должно оканчиваться на .json"
        )

    object_report_id = file_name[:-5]
    if report_info["id"] != object_report_id:
        raise ValueError(
            f"{object_id}: ID отчёта {report_info['id']!r} "
            f"не совпадает с именем объекта {object_report_id!r}"
        )


def validate_task_pallet_assignment(
    s3,
    bucket_id: str,
    report_info: Mapping,
) -> dict:
    """Сверить SSCC с заданием и дополнить паллетом отчёт v1."""

    tasks_prefix = os.environ.get(
        "EQUIPMENT_TASKS_PREFIX",
        DEFAULT_TASKS_PREFIX,
    ).strip("/")
    tasks_bucket = os.environ.get(
        "EQUIPMENT_TASKS_BUCKET",
        bucket_id,
    )
    task_key = f"{tasks_prefix}/{report_info['id']}.json"
    task = _read_optional_s3_json(s3, tasks_bucket, task_key)

    if task is None:
        if report_info["schema_version"] == 2:
            raise ValueError(
                f"{tasks_bucket}/{task_key}: задание не найдено"
            )
        logger.info(
            "Legacy v1 report %s has no equipment task; "
            "sending summary without pallet label",
            report_info["id"],
        )
        return dict(report_info)

    raw_assigned = task.get("palletNumbers")
    task_schema_version = task.get("reportSchemaVersion")

    if task.get("id") != report_info["id"]:
        raise ValueError(
            f"{tasks_bucket}/{task_key}: ID задания {task.get('id')!r} "
            f"не совпадает с ID отчёта {report_info['id']!r}"
        )

    # Старые задания не содержали признаков паллетной агрегации. Для них
    # сохраняем прежний формат уведомления v1 без ярлыка.
    if (
        report_info["schema_version"] == 1
        and task_schema_version in (None, 1)
        and raw_assigned in (None, [])
    ):
        logger.info(
            "Legacy v1 task %s has no palletNumbers; "
            "sending summary without pallet label",
            task_key,
        )
        return dict(report_info)

    if task_schema_version != report_info["schema_version"]:
        raise ValueError(
            f"{tasks_bucket}/{task_key}: reportSchemaVersion должен быть "
            f"равен {report_info['schema_version']}"
        )

    if not isinstance(raw_assigned, list) or not raw_assigned:
        raise ValueError(
            f"{tasks_bucket}/{task_key}: "
            "palletNumbers должен быть непустым массивом"
        )

    assigned = {
        normalize_sscc(value, f"{task_key}.palletNumbers")
        for value in raw_assigned
    }
    if len(assigned) != len(raw_assigned):
        raise ValueError(
            f"{tasks_bucket}/{task_key}: palletNumbers содержит дубли"
        )

    if report_info["schema_version"] == 1:
        if len(raw_assigned) != 1:
            raise ValueError(
                f"{tasks_bucket}/{task_key}: для отчёта v1 должен быть "
                "назначен ровно один palletNumbers"
            )
        pallet_number = next(iter(assigned))
        enriched = dict(report_info)
        enriched.update(
            {
                "pallets": 1,
                "pallet_numbers": [pallet_number],
                "pallet_details": [
                    {
                        "pallet_number": pallet_number,
                        "boxes": report_info["boxes"],
                        "products": report_info["products"],
                        "ssccs": report_info["ssccs"],
                    }
                ],
            }
        )
        return enriched

    report_numbers = report_info["pallet_numbers"]
    if len(set(report_numbers)) != len(report_numbers):
        raise ValueError("Отчёт содержит повторяющийся SSCC паллета")
    for pallet_number in report_numbers:
        if pallet_number not in assigned:
            raise ValueError(
                f"Паллет {pallet_number} не назначен в {task_key}"
            )
    return dict(report_info)


def _get_fonts():
    local_font_path = os.path.join(
        os.path.dirname(__file__),
        "DejaVuSans.ttf",
    )
    paths = [
        local_font_path,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/ttf/dejavu/DejaVuSans.ttf",
    ]
    font_path = next((path for path in paths if os.path.exists(path)), None)
    if font_path:
        return {
            40: ImageFont.truetype(font_path, 40),
            32: ImageFont.truetype(font_path, 32),
            28: ImageFont.truetype(font_path, 28),
            24: ImageFont.truetype(font_path, 24),
        }
    default = ImageFont.load_default()
    return {40: default, 32: default, 28: default, 24: default}


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    """Перенести длинный текст по пробелам, дефисам или символам."""
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return [text]

    tokens = text.split(" ")
    if len(tokens) == 1:
        tokens = re.findall(r"[^-]+-?", text)

    lines = []
    current = ""
    for token in tokens:
        separator = " " if current and " " in text else ""
        candidate = f"{current}{separator}{token}".strip()
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = token
        else:
            current = candidate

    if current:
        lines.append(current)
    return lines or [text]


def create_info_image(report_data: Mapping) -> bytes:
    """Создать мобильную PNG-карточку отчёта."""
    lines = [
        ("ОТЧЁТ ОБОРУДОВАНИЯ", True),
        (f"ID: {report_data['id']}", False),
        (f"Оператор: {report_data['operator']}", False),
    ]
    if report_data.get("pallets"):
        lines.append((f"Паллетов: {report_data['pallets']}", False))
    lines.extend(
        [
            (f"Коробок: {report_data['boxes']}", False),
            (f"Продуктов: {report_data['products']}", False),
        ]
    )
    if report_data.get("ssccs"):
        lines.append((f"Номера коробов: {report_data['ssccs']}", False))

    fonts = _get_fonts()
    title_font = fonts[32]
    body_font = fonts[24]
    width = 600
    padding = 40
    line_spacing = 15
    draw_test = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    prepared_lines = []
    total_height = padding * 2
    for text, is_title in lines:
        font = title_font if is_title else body_font
        for wrapped in _wrap_text(
            draw_test,
            text,
            font,
            width - padding * 2,
        ):
            bbox = draw_test.textbbox((0, 0), wrapped, font=font)
            height = bbox[3] - bbox[1]
            prepared_lines.append((wrapped, font, height))
            total_height += height + line_spacing

    total_height -= line_spacing
    image = Image.new("RGB", (width, total_height), color="white")
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        [0, 0, width - 1, total_height - 1],
        outline=(200, 200, 200),
        width=4,
    )

    current_y = padding
    for text, font, height in prepared_lines:
        draw.text((padding, current_y), text, font=font, fill=(30, 30, 30))
        current_y += height + line_spacing

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def create_pallet_label_image(
    *,
    report_id: str,
    operator: str,
    pallet_number: str,
    pallet_index: int,
    pallet_count: int,
    boxes_count: int,
    products_count: int,
) -> bytes:
    """Создать печатный ярлык паллета с корректным GS1-128 (AI 00)."""
    sscc = normalize_sscc(pallet_number, "palletNumber")
    barcode_class = get_barcode_class("gs1_128")
    barcode_buffer = io.BytesIO()
    barcode_class(
        pallet_gs1_value(sscc),
        writer=ImageWriter(),
    ).write(
        barcode_buffer,
        options={
            "format": "PNG",
            "dpi": 300,
            "module_width": 0.5,
            "module_height": 28,
            "quiet_zone": 5,
            "write_text": False,
            "background": "white",
            "foreground": "black",
        },
    )
    barcode_image = Image.open(barcode_buffer).convert("RGB")

    width = max(1200, barcode_image.width + 120)
    padding = 60
    fonts = _get_fonts()
    title_font = fonts[40]
    body_font = fonts[28]
    sscc_font = fonts[32]
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    detail_lines = []
    for detail in (
        f"Отчёт: {report_id}",
        f"Оператор: {operator}",
    ):
        detail_lines.extend(
            _wrap_text(
                probe,
                detail,
                body_font,
                width - padding * 2,
            )
        )
    count_lines = [
        f"Коробов: {boxes_count}",
        f"Штук: {products_count}",
    ]
    line_height = 38
    total_height = (
        padding
        + 55
        + 25
        + len(detail_lines) * line_height
        + 25
        + len(count_lines) * line_height
        + 15
        + barcode_image.height
        + 25
        + 45
        + padding
    )

    label = Image.new("RGB", (width, total_height), color="white")
    draw = ImageDraw.Draw(label)
    draw.rectangle(
        [0, 0, width - 1, total_height - 1],
        outline="black",
        width=5,
    )

    title = f"ПАЛЛЕТА {pallet_index + 1} ИЗ {pallet_count}"
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    draw.text(
        ((width - (title_bbox[2] - title_bbox[0])) / 2, padding),
        title,
        font=title_font,
        fill="black",
    )

    current_y = padding + 80
    for line in detail_lines:
        line_bbox = draw.textbbox((0, 0), line, font=body_font)
        draw.text(
            ((width - (line_bbox[2] - line_bbox[0])) / 2, current_y),
            line,
            font=body_font,
            fill="black",
        )
        current_y += line_height

    current_y += 10
    for line in count_lines:
        line_bbox = draw.textbbox((0, 0), line, font=body_font)
        draw.text(
            ((width - (line_bbox[2] - line_bbox[0])) / 2, current_y),
            line,
            font=body_font,
            fill="black",
        )
        current_y += line_height

    current_y += 10
    label.paste(
        barcode_image,
        ((width - barcode_image.width) // 2, current_y),
    )
    current_y += barcode_image.height + 20

    human_text = f"(00) {sscc}"
    human_bbox = draw.textbbox((0, 0), human_text, font=sscc_font)
    draw.text(
        ((width - (human_bbox[2] - human_bbox[0])) / 2, current_y),
        human_text,
        font=sscc_font,
        fill="black",
    )

    output = io.BytesIO()
    label.save(output, format="PNG", dpi=(300, 300))
    return output.getvalue()


def build_message_text(report_info: Mapping) -> str:
    """Сформировать текст сводного сообщения MAX."""
    lines = [
        "Отчёт оборудования",
        f"ID: {report_info['id']}",
        f"Оператор: {report_info['operator']}",
    ]
    if report_info.get("pallets"):
        lines.append(f"Количество паллетов: {report_info['pallets']}")
    lines.extend(
        [
            f"Количество коробок: {report_info['boxes']}",
            f"Количество продуктов: {report_info['products']}",
        ]
    )
    if report_info.get("ssccs"):
        lines.append(f"Номера коробов: {report_info['ssccs']}")
    return "\n".join(lines)


def _safe_file_part(value: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-я_.-]+", "_", value)[:120]


def send_file_message(
    *,
    token: str,
    chat_id: str,
    text: str,
    file_name: str,
    file_bytes: bytes,
) -> bool:
    """Загрузить PNG и отправить сообщение, ожидая готовности вложения."""
    auth_headers = {"Authorization": token}
    init_response = requests.post(
        MAX_UPLOAD_URL,
        params={"type": "file"},
        headers=auth_headers,
        timeout=10,
    )
    init_response.raise_for_status()
    upload_url = init_response.json().get("url")
    if not upload_url:
        raise ValueError(
            f"MAX не вернул URL загрузки: {init_response.text}"
        )

    upload_response = requests.post(
        upload_url,
        headers=auth_headers,
        files={"file": (file_name, file_bytes, "image/png")},
        timeout=30,
    )
    upload_response.raise_for_status()
    file_token = upload_response.json().get("token")
    if not file_token:
        raise ValueError("MAX не вернул token после загрузки файла")

    payload = {
        "text": text,
        "attachments": [
            {
                "type": "file",
                "payload": {"token": file_token},
            }
        ],
        "notify": True,
    }
    message_headers = {
        "Authorization": token,
        "Content-Type": "application/json",
    }

    for attempt in range(10):
        response = requests.post(
            MAX_API_URL,
            params={"chat_id": chat_id},
            json=payload,
            headers=message_headers,
            timeout=20,
        )
        if response.status_code == 200:
            logger.info(
                "MAX message sent on attempt %s",
                attempt + 1,
            )
            return True

        try:
            response_data = response.json()
        except ValueError:
            response_data = {}

        if response_data.get("code") != "attachment.not.ready":
            logger.error(
                "MAX API error %s: %s",
                response.status_code,
                response.text,
            )
            return False

        wait_time = (attempt + 1) * 2
        logger.info(
            "MAX attachment is not ready; waiting %s seconds",
            wait_time,
        )
        time.sleep(wait_time)

    logger.error("MAX attachment was not ready after 10 attempts")
    return False


def handler(event, context):
    """Обработать новые equipment reports v1/v2 из Object Storage."""
    token = os.environ.get("MAX_BOT_TOKEN")
    chat_id = os.environ.get("MAX_CHAT_ID")
    if not token or not chat_id:
        logger.error(
            "Missing MAX_BOT_TOKEN or MAX_CHAT_ID environment variables"
        )
        return {"statusCode": 500, "body": "Configuration error"}

    s3 = get_s3_client()

    for message in event.get("messages", []):
        details = message.get("details", {})
        bucket_id = details.get("bucket_id")
        object_id = details.get("object_id")
        if not bucket_id or not object_id:
            continue

        try:
            logger.info(
                "Processing file %s from bucket %s",
                object_id,
                bucket_id,
            )
            report = _read_s3_json(s3, bucket_id, object_id)
            report_info = extract_report_info(report)
            validate_report_identity(object_id, report_info)
            report_info = validate_task_pallet_assignment(
                s3,
                bucket_id,
                report_info,
            )
            logger.info(
                "Validated report %s: schema=v%s pallets=%s boxes=%s "
                "products=%s",
                report_info["id"],
                report_info["schema_version"],
                report_info["pallets"],
                report_info["boxes"],
                report_info["products"],
            )

            # Исторический v1 не имеет назначенного паллета, поэтому для него
            # сохраняется прежняя сводка. Паллетизированный отчёт не должен
            # дублироваться сводкой: вся необходимая информация уже входит в
            # сообщение с этикеткой каждого паллета.
            if not report_info["pallet_details"]:
                report_id_for_file = _safe_file_part(report_info["id"])
                summary_sent = send_file_message(
                    token=token,
                    chat_id=chat_id,
                    text=build_message_text(report_info),
                    file_name=f"report_{report_id_for_file}.png",
                    file_bytes=create_info_image(report_info),
                )
                if not summary_sent:
                    logger.error(
                        "Summary notification failed for report %s",
                        report_info["id"],
                    )
                continue

            pallet_count = report_info["pallets"]
            for pallet_index, pallet_info in enumerate(
                report_info["pallet_details"]
            ):
                pallet_number = pallet_info["pallet_number"]
                try:
                    label_text_lines = [
                        f"Ярлык паллета "
                        f"{pallet_index + 1}/{pallet_count}",
                        f"Отчёт: {report_info['id']}",
                        f"Оператор: {report_info['operator']}",
                        f"SSCC: {pallet_number}",
                        f"Коробов: {pallet_info['boxes']}",
                        f"Штук: {pallet_info['products']}",
                    ]
                    if pallet_info.get("ssccs"):
                        label_text_lines.append(
                            f"Номера коробов: {pallet_info['ssccs']}"
                        )
                    label_sent = send_file_message(
                        token=token,
                        chat_id=chat_id,
                        text="\n".join(label_text_lines),
                        file_name=(
                            f"pallet_{pallet_index + 1}_"
                            f"{pallet_number}.png"
                        ),
                        file_bytes=create_pallet_label_image(
                            report_id=report_info["id"],
                            operator=report_info["operator"],
                            pallet_number=pallet_number,
                            pallet_index=pallet_index,
                            pallet_count=pallet_count,
                            boxes_count=pallet_info["boxes"],
                            products_count=pallet_info["products"],
                        ),
                    )
                    if not label_sent:
                        logger.error(
                            "Pallet label notification failed: "
                            "report=%s sscc=%s",
                            report_info["id"],
                            pallet_number,
                        )
                except Exception as error:
                    logger.exception(
                        "Pallet label notification error: "
                        "report=%s sscc=%s error=%s",
                        report_info["id"],
                        pallet_number,
                        error,
                    )

        except Exception as error:
            logger.exception(
                "Critical error processing object %s: %s",
                object_id,
                error,
            )

    return {"statusCode": 200, "body": "OK"}
