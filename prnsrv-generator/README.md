# prnsrv-generator

Yandex Cloud Function, заменяющая polling-обработчик `prnsrv` для генерации CSV/VDF. Бизнес-логика не копируется в этот каталог: функция импортирует отдельный Python-пакет [`prnsrv`](https://github.com/yankoval/prnsrv).

Точка входа Cloud Functions: `index.handler`.

## Контракт обработки

1. Object Storage trigger передаёт `Задания/<UUID>.json`.
2. `count` имеет приоритет над legacy-полем `Quantity`.
3. `int(count) <= 0` либо `TypeError`/`ValueError`/`OverflowError` означает штатный `NO_PRINT`:
   - SSCC allocator не вызывается;
   - CSV/VDF не создаются;
   - создаётся `_prnsrv/done/<UUID>.done`;
   - пишется только INFO и invocation завершается success.
4. Для положительного количества allocator обязан быть идемпотентным по `UUID + source_hash`.
5. CSV и VDF записываются с `If-None-Match: *`; существующее содержимое принимается только при совпадении SHA-256.
6. CSV публикуется раньше VDF. `done.result=GENERATED` создаётся только после повторной проверки обоих outputs.

В функции нет state/lock, S3 tags и report objects.

## Переменные окружения

| Имя | Обязательность | По умолчанию |
|---|---|---|
| `BUCKET_ID` | Для timer reconciliation | — |
| `INPUT_PREFIX` | Нет | `Задания/` |
| `OUTPUT_PREFIX` | Нет | `printer-tasks/` |
| `DONE_PREFIX` | Нет | `_prnsrv/done/` |
| `TEMPLATES_BUCKET` | Нет | bucket исходного задания |
| `TEMPLATES_PREFIX` | Нет | `printer-templates/` |
| `MAPPING_KEY` | Нет | bundled mapping из пакета `prnsrv` |
| `SSCC_URL` | Для `count > 0` | — |
| `SSCC_PREFIX` | Нет | `460705179` |
| `SSCC_EXTENSION` | Нет | `0` |
| `SSCC_TIMEOUT_SECONDS` | Нет | `15` |
| `COLUMN_NAME` | Нет | `C1` |
| `WINDOWS_CSV_DIR` | Нет | `C:\tmp` |
| `RECONCILE_HOURS` | Нет | `48` |
| `STALE_AFTER_HOURS` | Нет | `1` |
| `S3_ENDPOINT` | Нет | `https://storage.yandexcloud.net` |
| `AWS_REGION` | Нет | `ru-central1` |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | Если SDK не получает credentials иным способом | — |

Секреты не должны попадать в ZIP или Git. Для production они передаются через Lockbox/переменные версии функции.

## Шаблоны

VDF-шаблон читается из:

```text
s3://<TEMPLATES_BUCKET>/<TEMPLATES_PREFIX><PasportData.Format>.vdf
```

Имя `PasportData.Format` проверяется как basename без `/`, `\`, `.` и `..`. Mapping по умолчанию включён в пакет `prnsrv`; `MAPPING_KEY` позволяет использовать versioned object в S3.

## Ежедневная сверка

Timer trigger вызывает тот же handler без Object Storage details. Функция один раз выполняет LIST `Задания/`, оставляет задания за последние 48 часов старше одного часа и вычитает LIST `_prnsrv/done/`.

Результат существует только в structured logs/metrics. Автоматического replay и S3 report object нет. Lifecycle `Expiration: 2 days` настраивается только для `_prnsrv/done/`.

## Локальные тесты

Из этого каталога:

```bash
PYTHONPATH=../../prnsrv:. python -m unittest discover -s tests -v
```

Отдельно тестируется пакет:

```bash
cd ../../prnsrv
python -m unittest discover -s tests -v
```

Тесты используют in-memory S3 и allocator; сеть и production не затрагиваются.

## Источник пакета и сборка ZIP

Главный release-источник — GitHub. Обычная сборка использует зафиксированный commit `prnsrv`, указанный в `scripts/build.py`:

```bash
python scripts/build.py
```

При обновлении `prnsrv` сначала публикуется новая ревизия репозитория, затем SHA меняется в сборщике. При необходимости конкретную Git-ревизию можно передать явно:

```bash
python scripts/build.py \
  --prnsrv-requirement 'git+https://github.com/yankoval/prnsrv.git@<commit-sha>'
```

Локальный checkout разрешён только как явный режим разработки и не является release-источником:

```bash
python scripts/build.py --prnsrv-source ../../prnsrv
```

Результат: `dist/prnsrv-generator.zip`. В корне архива находятся `index.py`, адаптер, зависимости и установленный пакет `prnsrv`; entrypoint остаётся `index.handler`.

Сборщик по умолчанию загружает wheels для `manylinux2014_x86_64` и CPython 3.14 (`python314` — поддерживаемый runtime Yandex Cloud Functions), а не для локальной macOS. Параметры можно явно задать через `--target-platform` и `--python-version`; они должны совпадать с runtime версии Cloud Function.

`build-manifest.json` фиксирует точную Git-зависимость. Для локального режима он дополнительно фиксирует commit и признак dirty worktree; такой архив не используется как production release.
