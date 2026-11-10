# services/whisper

Self-hosted распознавание. Платного API речи нет. В образ входят **faster-whisper** и **ffmpeg**: в n8n ffmpeg нет, поэтому и нарезка, и транскрипт делаются здесь.

Модель по умолчанию — `large-v3-turbo`, квантование `int8`. Запасная — `large-v3`, её включают переменной `WHISPER_MODEL` или полем `model` в одном запросе. Сервер расчётный: 8 vCPU, без GPU, около 5 ГБ свободной RAM на время работы контейнера. В памяти держится одна модель. Контейнер живёт постоянно: через 15 минут без тяжёлых запросов модель выгружается из RAM (`WHISPER_IDLE_UNLOAD_SEC`), процесс остаётся и следующий `/transcribe` грузит её заново из volume. Выход по простою (`WHISPER_IDLE_EXIT_SEC`) по умолчанию выключен: у n8n нет docker.sock и SSH к хосту, поднять контейнер он не может.

Большие лекции сервис качает из Google Drive сам (`/drive/fetch`), n8n видео в память не берёт. Дизайн — [docs/ingest_large_files.md](../../docs/ingest_large_files.md).

## Контракт

Базовый URL задаёт `WHISPER_BASE_URL`. Тело запросов — JSON. Файлы по HTTP не загружаются: в запросе путь внутри `/data` контейнера (относительный путь считается от `/data`). n8n этот каталог не монтирует и файлы не читает: медиа попадает туда через `/drive/fetch`. Путь вне `DATA_DIR` отвечает `400`.

Если задан `WHISPER_TOKEN`, `/extract-audio`, `/transcribe` и `/files/delete` требуют заголовок `Authorization: Bearer <токен>` или `X-Whisper-Token: <токен>`. `/drive/*` вместо этого требуют токен Google в `Authorization` (см. ниже). `/health` токен не проверяет. Без токена сервис можно держать только во внутренней сети Dokploy, порт наружу не публиковать.

Ошибка — JSON `{"error":"..."}`. Коды: `400` путь или файл негодный, `401` токен, `404` файла нет, `403` Drive не дал доступ, `507` не хватает диска, `502` ошибка Drive, `409` уже идёт другая нарезка или распознавание, `422` кривое тело, `504` ffmpeg не уложился в `EXTRACT_TIMEOUT_SEC`.

В лог не пишутся текст транскрипта, `initial_prompt` и имя файла. Пишутся короткий хеш пути, модель, длительность и число сегментов.

### `GET /health`

```json
{
  "status": "ok",
  "ffmpeg": true,
  "model": "large-v3-turbo",
  "compute_type": "int8",
  "device": "cpu",
  "model_loaded": false,
  "loaded_model": null
}
```

`model_loaded: false` — нормально до первого `/transcribe`. Модель на health не грузится. `503`, если нет ffmpeg или устройство не `cpu`.

### `POST /extract-audio`

Вход:

```json
{"input_path": "/data/inbox/lecture.mp4", "output_path": "/data/audio/lecture.16k.opus"}
```

`output_path` можно не передавать: рядом с исходником появится `<имя>.16k.opus`. Существующий выход перезаписывается. `"delete_input": true` удаляет исходник после успешной нарезки (в ответе `input_deleted`).

Выход:

```json
{
  "output_path": "/data/audio/lecture.16k.opus",
  "duration_sec": 1440.0,
  "channels": 1,
  "codec": "opus",
  "sample_rate": 16000,
  "container_sample_rate": 48000,
  "elapsed_sec": 12.4
}
```

Сигнал перед кодеком — mono 16 kHz, битрейт opus 24 кбит/с (ориентир из плана: около 5 МБ на 24 минуты). У Opus кадры часто лежат на часах 48 kHz, поэтому ffprobe может показать `48000`. Это поле `container_sample_rate`. `sample_rate: 16000` — частота, которую задали ffmpeg. Перед моделью файл ещё раз декодируется в mono 16 kHz float32.

### `POST /transcribe`

Вход:

```json
{
  "audio_path": "/data/audio/lecture.16k.opus",
  "initial_prompt": "термин один, термин два",
  "language": "ru",
  "model": "large-v3-turbo"
}
```

`language` по умолчанию `ru`. `model` — необязательный override: `large-v3-turbo` или `large-v3`. В тестах ещё разрешён `tiny`; в проде его не ставьте. `initial_prompt` — канонические написания из `glossary_terms`, не текст лекции. Длиннее 800 символов обрезается по запятой, в ответе будет `initial_prompt_truncated: true`. Это тот же потолок, что в плане: около 200 токенов Whisper.

`vad_filter` по умолчанию включён (тишина в лекции). `beam_size` по умолчанию 5, максимум 5.

Выход:

```json
{
  "text": "полный текст",
  "segments": [
    {"ordinal": 1, "start_ms": 0, "end_ms": 1400, "text": "фраза"}
  ],
  "model": "large-v3-turbo",
  "compute_type": "int8",
  "language": "ru",
  "duration_sec": 1440.0,
  "elapsed_sec": 90.5,
  "model_load_sec": 20.0,
  "transcribe_sec": 70.5,
  "initial_prompt_truncated": false
}
```

Сегмент пишется в `chunks`: `ordinal`, `start_ms`, `end_ms`, `text`, `lecture_id`. `lecture_texts.transcript` — поле `text`. `lecture_texts.whisper_model` — поле `model`. `lectures.duration_sec` — `duration_sec`, округлённый до секунды на стороне n8n.

Повторный вызов с другой моделью выгружает предыдущую. Две модели в RAM одновременно не живут.

### `POST /drive/list-media`

Заголовок `Authorization: Bearer <OAuth access token Google>`. В n8n его подставляет HTTP Request с predefined credential `Google Drive OAuth2 API`. Вход `{"folder_id": "...", "depth": 1}`: медиа (`video/*`, `audio/*`) в папке и в подпапках до глубины `depth` (0–3). Выход:

```json
{"folder_id": "...", "folders_scanned": 2, "files": [
  {"id": "...", "name": "lecture.mp4", "mime_type": "video/mp4", "media_kind": "video",
   "size_bytes": 2500000000, "md5": "...", "created_time": "...", "modified_time": "...",
   "duration_ms": 5400000, "parent_id": "...", "folder_path": ["Лекция 06"]}
]}
```

`401 google token rejected` — токен истёк. n8n на такой ответ обновляет токен credential и повторяет запрос.

### `POST /drive/fetch`

Тот же заголовок Google. Вход `{"file_id": "...", "output_path": "inbox/<lecture_id>.mp4"}`. Сервис стримит файл в `<output_path>.part`, сверяет размер и `md5Checksum`, проверяет свободное место (`size + WHISPER_MIN_FREE_MB`) и переименовывает. Выход: `output_path`, `size_bytes`, `md5_ok`, `file` (метаданные как в списке), `elapsed_sec`. Занимает тот же замок, что нарезка и распознавание: параллельно `409`.

### `POST /files/delete`

`{"path": "audio/x.16k.opus"}` → `{"path": "...", "deleted": true}`. Только внутри `/data`. Удаляет и хвост `.part`.

## Переменные

| Имя | По умолчанию | Смысл |
|---|---|---|
| `WHISPER_MODEL` | `large-v3-turbo` | Модель, если в запросе своя не передана |
| `WHISPER_COMPUTE_TYPE` | `int8` | `int8`, `int8_float32`, `int16`, `float32` |
| `WHISPER_DEVICE` | `cpu` | Другое значение сервис отвергает |
| `WHISPER_CPU_THREADS` | `8` | Потоков CTranslate2, не больше 8 |
| `WHISPER_CACHE_DIR` | `/models` | Каталог весов. Его и монтировать volume |
| `DATA_DIR` | `/data` | Общий каталог с n8n |
| `WHISPER_BEAM_SIZE` | `5` | Ширина луча, потолок 5 |
| `WHISPER_VAD_FILTER` | `1` | `0` выключает вырезание тишины |
| `EXTRACT_TIMEOUT_SEC` | `7200` | Потолок ffmpeg |
| `WHISPER_MAX_AUDIO_SEC` | `14400` | Потолок длины на распознавание (4 часа) |
| `WHISPER_IDLE_UNLOAD_SEC` | `900` | Простой до выгрузки модели из RAM. `0` — не выгружать |
| `WHISPER_IDLE_EXIT_SEC` | `0` | Простой до выхода процесса. `0` — не выходить |
| `WHISPER_MIN_FREE_MB` | `3072` | Запас диска сверх размера файла для `/drive/fetch` |
| `DRIVE_API_BASE` | `https://www.googleapis.com/drive/v3` | Меняется только в тестах |
| `DRIVE_TIMEOUT_SEC` | `120` | Таймаут сокета к Drive |
| `WHISPER_TOKEN` | пусто | Токен на `/extract-audio`, `/transcribe`, `/files/delete` |

`GET /health` простой не сбрасывает. Опрос живости Dokploy контейнер сам по себе не держит.

Ориентир из плана: int8 на 8 CPU около 0.15 длительности записи плюс загрузка модели в память на первом запросе после старта. Таймаут HTTP в n8n для часовой лекции поставьте с запасом (от 20 минут) и поправьте после первого замера на этой машине. Первый запрос ещё и скачивает веса, если volume пустой.

## Запуск

### Сервер (прод)

Отдельный compose-проект вне приложений Dokploy: [deploy/docker-compose.server.yml](deploy/docker-compose.server.yml) → `/opt/psy-whisper/docker-compose.yml`, код в `/opt/psy-whisper/src`, медиа в `/opt/psy-whisper/data`, `WHISPER_TOKEN` в `/opt/psy-whisper/.env` (chmod 600, в git нет). Контейнер `psy-whisper` подключён к сети `myn8n-n8n-iucjks`, порт наружу не опубликован. Для n8n: `http://psy-whisper:8080`. В n8n токен лежит в credential `psy Whisper token` (Bearer, домен ограничен `psy-whisper`).

```bash
rsync -a --exclude tests --exclude deploy services/whisper/ vps-dokploy:/opt/psy-whisper/src/
scp services/whisper/deploy/docker-compose.server.yml vps-dokploy:/opt/psy-whisper/docker-compose.yml
ssh vps-dokploy 'cd /opt/psy-whisper && docker compose up -d --build'
ssh vps-dokploy 'docker exec myn8n-n8n-iucjks-n8n-1 wget -qO- http://psy-whisper:8080/health'
```

Порядок для n8n:

1. `GET /health`, ждать `status: ok` и `busy: false`.
2. `POST /drive/fetch` (credential Google Drive), затем `POST /extract-audio` с `delete_input: true`, затем `POST /transcribe` (credential `psy Whisper token`). В промпт — `canonical` из `glossary_terms` курса через запятую.
3. На `409` — вернуть job в очередь. Модель сама выгрузится через 15 минут.

### Локально

```bash
cd services/whisper
export DATA_DIR_HOST=/абсолютный/путь/к/data
docker compose up --build -d
curl -s http://127.0.0.1:8080/health
```

`docker-compose.yml` в каталоге — для локального смоука с портом на `127.0.0.1`.

Домены для первой загрузки весов: `huggingface.co` и его CDN. Веса лежат в volume `psy-whisper-models` и переживают пересборку.

`large-v3` тяжелее turbo. На коробке с ~5 ГБ свободной RAM запасная модель может не влезть, если рядом живёт n8n. Тогда оставляйте turbo.

## Как проверить без лекции

Нужны ffmpeg, espeak-ng и Python 3.12. Тест сам синтезирует фразу «проверка сервиса» и не кладёт аудио в git.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r services/whisper/requirements.txt -r services/whisper/requirements-dev.txt
pytest services/whisper/tests -q
```

Тест распознавания ставит `WHISPER_MODEL=tiny`, чтобы не скачивать turbo. Образ при пустом env всё равно поднимает `large-v3-turbo`.

Проверка на сервере после сборки — `GET /health`, затем те же два POST на коротком файле в общем `/data`, не на записи курса. Первый `/transcribe` с моделью по умолчанию долгий: качается и грузится turbo.

Образ собран на сервере, pytest прогнан внутри образа (`tests/test_api.py`, `tests/test_drive_and_idle.py`; Drive подменён локальным HTTP-сервером).
