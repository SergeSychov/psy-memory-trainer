# services/whisper

Self-hosted распознавание. Платного API речи нет. В образ входят **faster-whisper** и **ffmpeg**: в n8n ffmpeg нет, поэтому и нарезка, и транскрипт делаются здесь.

Модель по умолчанию — `large-v3-turbo`, квантование `int8`. Запасная — `large-v3`, её включают переменной `WHISPER_MODEL` или полем `model` в одном запросе. Сервер расчётный: 8 vCPU, без GPU, около 5 ГБ свободной RAM на время работы контейнера. В памяти держится одна модель. Контейнер рассчитан на запуск по требованию: через 15 минут без `/extract-audio` и `/transcribe` процесс завершается, веса остаются в volume.

## Контракт

Базовый URL задаёт `WHISPER_BASE_URL`. Тело запросов — JSON. Файлы по HTTP не загружаются: n8n и контейнер монтируют один каталог в `/data`, в запросе путь внутри этого каталога. Путь вне `DATA_DIR` отвечает `400`.

Если задан `WHISPER_TOKEN`, `/extract-audio` и `/transcribe` требуют заголовок `Authorization: Bearer <токен>`. `/health` токен не проверяет. Без токена сервис можно держать только во внутренней сети Dokploy, порт наружу не публиковать.

Ошибка — JSON `{"error":"..."}`. Коды: `400` путь или файл негодный, `401` токен, `404` файла нет, `409` уже идёт другая нарезка или распознавание, `422` кривое тело, `504` ffmpeg не уложился в `EXTRACT_TIMEOUT_SEC`.

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

`output_path` можно не передавать: рядом с исходником появится `<имя>.16k.opus`. Существующий выход перезаписывается.

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
| `WHISPER_IDLE_EXIT_SEC` | `900` | Простой до выхода процесса. `0` — не выходить |
| `WHISPER_TOKEN` | пусто | Bearer-токен на два POST |

`GET /health` простой не сбрасывает. Опрос живости Dokploy контейнер сам по себе не держит.

Ориентир из плана: int8 на 8 CPU около 0.15 длительности записи плюс загрузка модели в память на первом запросе после старта. Таймаут HTTP в n8n для часовой лекции поставьте с запасом (от 20 минут) и поправьте после первого замера на этой машине. Первый запрос ещё и скачивает веса, если volume пустой.

## Запуск

Из каталога `services/whisper`, на машине с Docker:

```bash
export DATA_DIR_HOST=/абсолютный/путь/к/общему/data
docker compose up --build -d
curl -s http://127.0.0.1:8080/health
```

`127.0.0.1` в compose — только смоук с той же машины. В Dokploy порт наружу уберите и подключите контейнер к сети n8n. Имя для n8n — `http://psy-whisper:8080`, если контейнер называется так. Лимит: 8 CPU и 5 ГБ. Если Dokploy не читает блок `deploy`, выставьте 5 ГБ в UI. `restart` оставьте `no`: иначе после тихого выхода Docker поднимет процесс снова и RAM не вернётся.

Порядок для n8n, когда workflow появится:

1. Запустить контейнер (`docker start psy-whisper` или кнопка Dokploy).
2. Подождать `GET /health` со статусом `ok`.
3. `POST /extract-audio`, затем `POST /transcribe`. В промпт — `canonical` из `glossary_terms` этой лекции через запятую.
4. Остановить контейнер или подождать 15 минут: процесс выйдет сам. Volume `whisper-models` не удалять.

Пока контейнер жив, повторная лекция модель из сети не качает.

Домены для первой загрузки весов: `huggingface.co` и его CDN. Если исходящий доступ закрыт, volume нужно наполнить заранее.

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

В этой среде Docker не был установлен, образ здесь не собирался. Контракт прогнан pytest против того же `app.py`, который копирует Dockerfile.
