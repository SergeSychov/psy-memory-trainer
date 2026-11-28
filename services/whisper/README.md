# services/whisper

Self-hosted распознавание. Платного API речи нет. В образ входят **faster-whisper** и **ffmpeg**: в n8n ffmpeg нет, поэтому и нарезка, и транскрипт делаются здесь.

Модель по умолчанию — `large-v3-turbo`, квантование `int8`. Запасная — `large-v3`, её включают переменной `WHISPER_MODEL` или полем `model` в одном запросе. Сервер расчётный: 8 vCPU, без GPU. Прод-контейнер ограничен **4 ГБ** и 7 CPU. В модель не отдаётся весь файл: `/transcribe` режет запись окнами около 10 минут, в RAM один кусок float32 плюс веса. В памяти держится одна модель. Контейнер живёт постоянно: через 15 минут без тяжёлых запросов модель выгружается из RAM (`WHISPER_IDLE_UNLOAD_SEC`), процесс остаётся и следующий `/transcribe` грузит её заново из volume. Выход по простою (`WHISPER_IDLE_EXIT_SEC`) по умолчанию выключен: у n8n нет docker.sock и SSH к хосту, поднять контейнер он не может.

Большие лекции сервис качает из Google Drive сам (`/drive/fetch`) или по публичной ссылке (`/source/fetch`: Яндекс Диск, Drive «по ссылке», прямой https), n8n видео в память не берёт. Малые файлы из чата принимает `/inbox/upload`. Дизайн — [docs/ingest_large_files.md](../../docs/ingest_large_files.md).

## Контракт

Базовый URL задаёт `WHISPER_BASE_URL`. Тело запросов — JSON. Файлы по HTTP не загружаются: в запросе путь внутри `/data` контейнера (относительный путь считается от `/data`). n8n этот каталог не монтирует и файлы не читает: медиа попадает туда через `/drive/fetch`. Путь вне `DATA_DIR` отвечает `400`.

Если задан `WHISPER_TOKEN`, `/extract-audio`, `/transcribe` и `/files/delete` требуют заголовок `Authorization: Bearer <токен>` или `X-Whisper-Token: <токен>` (без токена в env эти старые ручки остаются открытыми — только для внутренней сети). `/source/probe`, `/source/fetch` и `/inbox/upload` всегда требуют непустой `WHISPER_TOKEN` в окружении **и** тот же заголовок: пустой токен → `401`. `/drive/*` вместо этого требуют токен Google в `Authorization` (см. ниже). `/health` токен не проверяет. Порт наружу не публиковать.

Ошибка — JSON `{"error":"..."}`. Коды: `400` путь или файл негодный, `401` токен, `404` файла нет, `403` Drive не дал доступ, `507` не хватает диска, `502` ошибка Drive, `409` уже идёт другая нарезка, распознавание, скачивание или слишком много параллельных проб, `422` кривое тело, `504` ffmpeg / дедлайн скачивания. У `/source/*` и `/inbox/upload` в `error` короткий код, см. таблицу ниже.

В лог не пишутся текст транскрипта, `initial_prompt`, имя файла и URL. Пишутся короткий хеш пути или ссылки, модель, длительность, размер и число сегментов. У источников по ссылке в лог попадает только хеш, вид источника, размер и короткая причина отказа (`blocked_address`, `scheme`, `html_instead_of_file` и т.п.).

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

`model_loaded: false` — нормально до первого `/transcribe`. Модель на health не грузится. `503`, если нет ffmpeg или устройство не `cpu`. Дополнительно: `chunk_sec`, `prompts_file_set`, `prompts_loaded` (файл курса прочитан и в нём есть непустой список).

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

Сигнал перед кодеком — mono 16 kHz, битрейт opus 24 кбит/с (ориентир из плана: около 5 МБ на 24 минуты). У Opus кадры часто лежат на часах 48 kHz, поэтому ffprobe может показать `48000`. Это поле `container_sample_rate`. `sample_rate: 16000` — частота, которую задали ffmpeg. Перед моделью каждое окно ещё раз декодируется в mono 16 kHz float32. Целиком файл в float32 не собирается: у faster-whisper и VAD, и мел-спектр считаются на весь переданный массив, на лекции 1,5–4 ч это выбивает контейнер из 4 ГБ.

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

`language` по умолчанию `ru`. `model` — необязательный override: `large-v3-turbo` или `large-v3`. В тестах ещё разрешён `tiny`; в проде его не ставьте. `initial_prompt` — канонические написания, не текст лекции. `prompt_key` — id курса: сервис подмешивает список из `WHISPER_PROMPTS_FILE` **перед** текстом запроса и убирает повторы. Длиннее 800 символов обрезается по запятой, в ответе будет `initial_prompt_truncated: true`. Это тот же потолок, что в плане: около 200 токенов Whisper. `hotwords` — необязательная строка для faster-whisper; если в запросе её нет, берётся `hotwords` этого курса из файла.

`vad_filter` по умолчанию включён (тишина в лекции). `beam_size` по умолчанию 5, максимум 5. `condition_on_previous_text` по умолчанию выключен (`WHISPER_CONDITION_ON_PREVIOUS_TEXT=0`): так модель реже зацикливается на одной фразе в паузе. Включить обратно — `1`.

Файл длиннее `WHISPER_CHUNK_SEC` (600 с) режется на окна. Рядом с границей ищется пауза (`ffmpeg silencedetect`, один проход, PCM не хранится) и разрез ставится в её середину. Если паузы нет — жёсткий стык с перекрытием `WHISPER_CHUNK_OVERLAP_SEC` (20 с): слово на границе попадает в оба окна и остаётся в одном, по середине сегмента. В ответе одна лента `segments` с абсолютными `start_ms`/`end_ms`, `ordinal` с 1. Поля сегмента те же: `ordinal`, `start_ms`, `end_ms`, `text`.

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
  "initial_prompt_truncated": false,
  "audio_pieces": [
    {"index": 0, "start_sec": 0, "end_sec": 600, "keep_from_sec": 0, "keep_to_sec": 590, "cut": "silence", "discarded_overlap": 0}
  ],
  "hallucination_filter": {
    "dropped_count": 1,
    "dropped_omitted": 0,
    "dropped": [
      {"start_ms": 120000, "end_ms": 123000, "text": "Продолжение следует...", "reason": "blocklist"}
    ],
    "condition_on_previous_text": false,
    "silence_threshold_sec": null
  }
}
```

Новые поля ответа добавляются к прежним. n8n по-прежнему берёт `text`, `segments`, `model`, `duration_sec`. `segments` не получают лишних ключей.

`hallucination_filter.dropped` — что снято уже после модели, чтобы это можно было сверить. `reason`: `blocklist` (известная заставка), `consecutive_duplicate` (тот же текст подряд), `boundary_duplicate` (тот же фрагмент на стыке окон). В список попадают не больше 100 записей, полное число — в `dropped_count`. Текст длиннее 500 символов в записи обрезается. В лог этот текст не пишется.

Блоклист по умолчанию — короткие заставки Whisper на тишине, не термины курса: «продолжение следует», «субтитры сделал/создал/добавил», «редактор субтитров», «спасибо за просмотр», «подписывайтесь на канал», `dimatorzok`. Срабатывает, только если вся реплика короче 180 символов: длинная фраза лекции, где эти слова встретились, остаётся. Свои фразы — `WHISPER_HALLUCINATION_PHRASES`, разделитель `|`. `WHISPER_HALLUCINATION_USE_DEFAULTS=0` оставляет только свой список.

`WHISPER_HALLUCINATION_SILENCE_THRESHOLD` по умолчанию выключен (`0`). Положительное число (обычно `2`) включает `word_timestamps` и отсев тишины внутри faster-whisper. Такие реплики в `dropped` не попадут: модель их не отдаёт. На CPU это заметно замедляет распознавание; для лекции на 4 часа таймаут n8n 180 минут может не хватить. Включать после замера RTF.

Сегмент пишется в `chunks`: `ordinal`, `start_ms`, `end_ms`, `text`, `lecture_id`. `lecture_texts.transcript` — поле `text`. `lecture_texts.whisper_model` — поле `model`. `lectures.duration_sec` — `duration_sec`, округлённый до секунды на стороне n8n. Для нескольких окон `duration_sec` — длина файла по ffprobe. Для одного окна, как раньше, берётся длительность, которую вернула модель.

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

### `POST /source/probe`

Лёгкая проверка источника перед постановкой лекции в очередь. Токен `WHISPER_TOKEN` **обязателен** (пустой в env → `401`). **Общий job-lock не занимает**. Одновременно не больше **3** проб (`409 busy` при переполнении). Общий таймаут 30 с.

Вход — одно из двух:

```json
{"kind": "direct_url", "url": "https://example.com/lecture.mp3"}
{"path": "inbox/lecture.mp3"}
```

`kind`: `yandex_public` (`disk.yandex.ru|com/d|i/<ключ>`, `yadi.sk/d|i/<ключ>`), `gdrive_public` (`drive.google.com/file/d/<ID>`, `open?id=`, `uc?id=`), `direct_url` (любой https). `path` — **только** файл внутри `inbox/` (не произвольный путь в `/data`).

Выход:

```json
{"name": "lecture.mp3", "size_bytes": 52428800, "mime": "audio/mpeg", "duration_sec": 5400.0, "resolved_kind": "audio"}
```

* `resolved_kind`: `audio`, `video` или `text` (`text/plain`, `.txt`, `.md`). Определяется по `Content-Type`, расширению и, если они молчат, по потокам ffprobe. Плейлистные MIME (`mpegurl`, `scpls`, `asx`, `dash+xml`) → `400 unsupported_type`. Не удалось: `400 unsupported_type`.
* `duration_sec` — best-effort, иначе `null`. Первые 8 МБ тела передаются в ffprobe через stdin (сам ffprobe в сеть не ходит: иначе он обошёл бы защиту от SSRF). Для mp4 с moov в конце и для потоков без заголовка длительности результата не будет. Если ffprobe длительности не дал, но знает битрейт, значение оценивается как `size / bit_rate`. Для `path` ffprobe читает файл целиком.
* `size_bytes` — `null`, если сервер не прислал размер.
* Яндекс: публичный API `cloud-api.yandex.net/v1/disk/public/resources`; папка (`type=dir`) → `400 unsupported_link`, нет ресурса → `403 not_public`.
* Drive: запрос без OAuth. Страница входа или HTML вместо файла → `403 not_public`. Для больших файлов делается единственный дополнительный шаг: тот же адрес с `confirm=t` (ссылки из HTML не читаются и не открываются).
* `direct_url` при HTTP `404`/`410` → `502 download_failed` (не `not_public`).

### `POST /source/fetch`

Скачивание в `/data`. Токен `WHISPER_TOKEN` **обязателен**. **Занимает общий замок** (`409 busy` при параллельной нарезке, распознавании или другом скачивании); при ошибке/таймауте замок **освобождается**.

```json
{"kind": "gdrive_public", "url": "https://drive.google.com/file/d/<ID>/view", "output_path": "inbox/<lecture_id>.mp4", "max_bytes": 4294967296}
```

`output_path` — только внутри `inbox/` (любая глубина, имя `[A-Za-z0-9._-]`, без `.part`). Лимит — меньшее из `max_bytes` и `SOURCE_MAX_BYTES`. Общий дедлайн скачивания `SOURCE_FETCH_MAX_SEC` (по умолчанию 3300 с) действует и на `open_url`, и на цикл чтения. Минимальная скорость: меньше `SOURCE_MIN_BYTES_PER_MIN` (по умолчанию 64 КиБ) за 60 с → `504 timeout`. Работа как у `/drive/fetch`: стрим кусками по 1 МБ в `<output_path>.part`, проверка `Content-Length` и фактического объёма (`413 too_large`), запас диска `size + WHISPER_MIN_FREE_MB` (`507`), совпадение числа байт с `Content-Length`, `rename`. После записи ffprobe проверяет `format_name`: `hls` / `concat` / `sdp` / `dash` / `image2` → `400 unsupported_type` (файл удаляется). Пустое тело → `502 download_failed`. При любой ошибке `.part` удаляется.

Выход: `output_path`, `size_bytes`, `name`, `mime`, `resolved_kind` (может быть `null`), `elapsed_sec`.

### `POST /inbox/upload?path=inbox/<имя>`

Сырое тело запроса (не JSON, не multipart) → файл `inbox/<имя>`. Для файлов из чата, которые n8n уже скачал через Bot API. Токен `WHISPER_TOKEN` **обязателен**. Лимит `INBOX_UPLOAD_MAX_BYTES` (25 МБ): по `Content-Length` и по фактическому объёму (`413 too_large`), тело пишется потоком в `.part`. Имя — один сегмент `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`, без `.part`, существующий файл перезаписывается; иное → `400`. Пустое тело → `400`. После записи — та же проверка `format_name` ffprobe, что у fetch. Замок не занимает.

Выход: `{"path": "/data/inbox/<имя>", "size_bytes": 12345}`.

### Коды ошибок источников

| `error` | HTTP | Когда |
|---|---|---|
| `unsupported_link` | 400 | Не та схема или порт, `user:pass@`, папка Drive/Яндекса, чужой хост у Яндекса/Drive, **адрес запрещён защитой от SSRF** (по причине клиенту не говорим, причина только в логе) |
| `not_public` | 403 | `401/403` от источника (и `404/410` у Яндекс/Drive), HTML вместо файла (страница входа, «нет доступа») |
| `unsupported_type` | 400 | Не аудио/видео/текст; плейлистный MIME; опасный `format_name` (`hls`/`concat`/`sdp`/`dash`/`image2`) |
| `too_large` | 413 | Размер больше лимита (по заголовку или по факту) |
| `download_failed` | 502 | DNS не резолвится, обрыв, `5xx`, больше 5 редиректов, битый сертификат, размер не сошёлся, пустое тело; у `direct_url` ещё `404`/`410` |
| `download_failed` | 429 | Источник ограничил частоту |
| `timeout` | 504 | Нет ответа за `SOURCE_TIMEOUT_SEC`; общий дедлайн `SOURCE_FETCH_MAX_SEC`; слишком медленное скачивание; у пробы ещё и общие 30 с |
| `busy` | 409 | `/source/fetch` при занятом замке; `/source/probe` при >3 параллельных пробах |
| `not enough disk space` | 507 | Нет места |

### Защита от SSRF

Контейнер whisper стоит в одной сети с n8n, поэтому ссылку пользователя нельзя открывать «как есть». Весь исходящий HTTP по ссылкам идёт через `safe_http.py`:

* только `https` и порт `443` (схемы `http`, `ftp`, `file`, `javascript` и другие, нестандартные порты и `user:pass@` отвергаются);
* имя резолвится в самом сервисе, проверяются **все** полученные адреса: хоть один запрещённый закрывает хост. Запрещены loopback, private (`10/8`, `172.16/12`, `192.168/16`), link-local (`169.254/16`, включая metadata облаков), CGNAT `100.64/10`, multicast, зарезервированные и документационные сети, IPv6 loopback, ULA `fc00::/7`, link-local `fe80::/10`, multicast, Teredo, а также `::ffff:a.b.c.d`, NAT64 и 6to4, у которых проверяется вложенный IPv4. Всё, что не является публичным адресом, тоже запрещено. Имена `localhost`, `*.local`, `*.internal`, `*.localhost` отвергаются до DNS. IP-литералы проходят ту же проверку (`https://127.0.0.1/` — отказ);
* **пиннинг IP**: TCP-соединение идёт на уже проверенный адрес, имя хоста остаётся только для `Host`, SNI и проверки сертификата, повторного резолва нет (защита от DNS rebinding);
* редиректы не следуются автоматически: максимум **5**, адрес каждого перепроверяется заново (схема, порт, DNS, все адреса), `https → http` отвергается;
* таймауты на соединение и чтение (`SOURCE_TIMEOUT_SEC`), общий дедлайн у `/source/probe`, `Accept-Encoding: identity`;
* `Content-Length` и фактический объём ≤ `max_bytes`;
* `text/html` вместо файла → `not_public`, ссылки внутри HTML не открываются (единственное исключение — шаг `confirm=t` для больших файлов Drive, адрес которого строит сам сервис).

Ослабить политику можно только из кода (`app.source_policy()`), так делают тесты. Переменной окружения для этого нет.

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
| `WHISPER_CHUNK_SEC` | `600` | Длина окна, секунды. Допустимо 1–900. Прод — 600 |
| `WHISPER_CHUNK_OVERLAP_SEC` | `20` | Перекрытие жёсткого стыка. `0` может срезать слово на границе |
| `WHISPER_SILENCE_NOISE_DB` | `-35` | Порог `silencedetect`, дБ |
| `WHISPER_SILENCE_MIN_SEC` | `0.5` | Короче этой паузы разрез не ставится |
| `WHISPER_SILENCE_SEARCH_SEC` | `90` | Где искать паузу у конца окна |
| `WHISPER_CONDITION_ON_PREVIOUS_TEXT` | `0` | `1` возвращает подсказку предыдущей репликой |
| `WHISPER_HALLUCINATION_SILENCE_THRESHOLD` | `0` | Секунды. `0` — выкл. `2` включает word timestamps |
| `WHISPER_HALLUCINATION_PHRASES` | пусто | Дополнительные фразы блоклиста, разделитель — вертикальная черта |
| `WHISPER_HALLUCINATION_USE_DEFAULTS` | `1` | `0` — только фразы из переменной выше |
| `WHISPER_PROMPTS_FILE` | пусто | JSON со списками курсов. Без файла работает запрос как раньше |
| `MKL_ENABLE_INSTRUCTIONS` | `AVX512` | Обход мусорных токенов int8 на Intel AMX. На AMD не мешает |
| `EXTRACT_TIMEOUT_SEC` | `7200` | Потолок ffmpeg |
| `WHISPER_MAX_AUDIO_SEC` | `14400` | Потолок длины на распознавание (4 часа) |
| `WHISPER_IDLE_UNLOAD_SEC` | `900` | Простой до выгрузки модели из RAM. `0` — не выгружать |
| `WHISPER_IDLE_EXIT_SEC` | `0` | Простой до выхода процесса. `0` — не выходить |
| `WHISPER_MIN_FREE_MB` | `3072` | Запас диска сверх размера файла для `/drive/fetch` |
| `DRIVE_API_BASE` | `https://www.googleapis.com/drive/v3` | Меняется только в тестах |
| `DRIVE_TIMEOUT_SEC` | `120` | Таймаут сокета к Drive |
| `WHISPER_TOKEN` | пусто | Старые ручки: если задан — нужен заголовок. `/source/*` и `/inbox/upload`: **обязателен** (пустой = `401`) |
| `SOURCE_MAX_BYTES` | `4294967296` (4 ГиБ) | Потолок размера для `/source/fetch`; запрос не может его превысить |
| `SOURCE_TIMEOUT_SEC` | `30` | Таймаут сокета (соединение и чтение) для `/source/*`, минимум 5 |
| `SOURCE_FETCH_MAX_SEC` | `3300` | Общий дедлайн одного `/source/fetch` (open + чтение), минимум 1 |
| `SOURCE_MIN_BYTES_PER_MIN` | `65536` (64 КиБ) | Минимум байт за 60 с чтения; иначе `504 timeout` |
| `INBOX_UPLOAD_MAX_BYTES` | `26214400` (25 МБ) | Потолок тела `/inbox/upload` |
| `SOURCE_YANDEX_API_BASE` | `https://cloud-api.yandex.net` | Меняется только в тестах |
| `SOURCE_GDRIVE_BASE` | `https://drive.google.com` | Меняется только в тестах |

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

1. `GET /health`, ждать `status: ok`, `busy: false`, `chunk_sec: 600`. `prompts_loaded: true` — файл курса прочитан.
2. `POST /drive/fetch` (credential Google Drive), затем `POST /extract-audio` с `delete_input: true`, затем `POST /transcribe` (credential `psy Whisper token`). В промпт — `canonical` из `glossary_terms` плюс список курса по `prompt_key` (id курса).
3. На `409` — вернуть job в очередь. Модель сама выгрузится через 15 минут.

### Списки курсов

Файл на сервере, не в git. Два курса — два ключа, ключ равен `psy.courses.id`.

```bash
ssh vps-dokploy 'mkdir -p /opt/psy-whisper/prompts && chmod 700 /opt/psy-whisper/prompts'
# скопировать services/whisper/prompts.example.json → /opt/psy-whisper/prompts/courses.json
# вписать initial_prompt (и при необходимости hotwords) и chmod 600
```

Форма как в [prompts.example.json](prompts.example.json): объект `courses`, у курса строки `initial_prompt` и `hotwords`. Пустая строка не считается. Строка вместо объекта тоже годится, это тот же `initial_prompt`. Пересборка контейнера после правки файла не нужна: файл читается на каждый `/transcribe`.

Тот же список можно положить в `psy.glossary_terms.canonical` (определение пустое). psy_03 и так собирает их в `initial_prompt`. Файл и глоссарий склеиваются, файл идёт первым, одинаковые написания не повторяются. Готовый файл в репозиторий не коммитить.

psy_03 в git (`psy_03_transcribe_v3`) передаёт `course_id` как `prompt_key` и остаётся `active: false`. На инстанс n8n этот JSON сам не уезжает. Пока workflow не обновлён, сервис принимает прежний запрос: глоссарий в `initial_prompt`, без файла.

### Проверка одной лекцией на сервере

После `docker compose up -d --build`. Лимиты контейнера не менялись: 4 ГБ, 7 CPU. Берётся уже готовый opus одной реальной лекции в `/opt/psy-whisper/data` (в git его не класть). Короткий смоук на синтетике эту проверку не заменяет.

В одном терминале, пока идёт распознавание:

```bash
ssh vps-dokploy 'docker stats psy-whisper --no-stream'
```

Повторять раз в несколько секунд или смотреть `docker stats` без `--no-stream`. Пик RSS должен остаться с запасом под 4 ГБ. Ориентир прошлого оконного прогона на этом ящике — около 1.8 ГБ.

Порт наружу не смотрит. Запрос из самого контейнера: токен уже в его окружении, в команду ssh его не подставлять. Подставьте путь opus и id курса. Ответ со всем текстом лекции на экран не выводите и в git не кладите.

```bash
ssh vps-dokploy 'docker exec -i psy-whisper python -' <<'PY'
import json, os, urllib.request
body = json.dumps({
    "audio_path": "/data/audio/LECTURE.16k.opus",
    "language": "ru",
    "prompt_key": "COURSE_UUID",
}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8080/transcribe",
    data=body,
    headers={
        "Authorization": "Bearer " + os.environ.get("WHISPER_TOKEN", ""),
        "Content-Type": "application/json",
    },
)
data = json.loads(urllib.request.urlopen(req, timeout=20000).read().decode())
print("duration_sec", data["duration_sec"])
print("transcribe_sec", data["transcribe_sec"])
print("rtf", round(data["transcribe_sec"] / data["duration_sec"], 3))
print("pieces", [(p["cut"], p["start_sec"], p["end_sec"]) for p in data["audio_pieces"]])
print("segments", len(data["segments"]))
print("dropped", data["hallucination_filter"]["dropped_count"])
print("filler", "Продолжение следует" in data["text"])
print("truncated", data["initial_prompt_truncated"])
PY
```

В ответе:

- `audio_pieces` — несколько окон, не одно на весь файл. `cut` чаще `silence`.
- `elapsed_sec` и `transcribe_sec`: RTF ≈ `transcribe_sec / duration_sec`. На прошлом оконном прогоне было около 0.52. Сильный рост значит, что включились word timestamps (`silence_threshold_sec` не `null`) или окно слишком длинное.
- В `text` нет «Продолжение следует». Если модель это написала, запись есть в `hallucination_filter.dropped` с `reason: blocklist`.
- `segments` идут по возрастанию `start_ms`, `ordinal` с 1, дыр на стыках окон нет: соседние реплики не повторяют одну и ту же фразу с пересечением по времени.
- `initial_prompt_truncated: true` — список курса не влез в 800 символов, хвост после последней запятой отрезан. Тогда список надо укоротить.

Память смотреть по пику `docker stats`, не по одному замеру после ответа: к этому моменту кусок уже отпущен.

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

`tests/test_sources.py` (источники по ссылке, `/inbox/upload`, SSRF) ставит тестовую политику с подменным резолвером и локальным HTTP-сервером, ffmpeg и модель не нужны. Проверка TLS с пиннингом использует `openssl` для самоподписанного сертификата и пропускается, если его нет.
