# Ingest больших лекций (2–3 ГБ) без загрузки в n8n

Статус: дизайн и сервисная часть готовы (`services/whisper`, задеплоен в `/opt/psy-whisper`). Workflow `psy_01`–`psy_03` собирает локальный Cursor по этому документу.

## Проблема

Лекция — видео 2–3 ГБ в Google Drive. Узел Google Drive → Download в n8n кладёт файл в binary data execution, то есть в память или в хранилище n8n. Для таких размеров это риск OOM и раздувания базы n8n. В n8n нет ffmpeg, нет docker.sock и нет SSH к хосту, и давать их ему не будем.

## Решение: Drive качает сервис whisper, токен даёт n8n

```
n8n (HTTP Request, credential "Google Drive account")
  └─ POST http://psy-whisper:8080/drive/fetch   Authorization: Bearer <OAuth-токен Google>
        сервис сам стримит файл из Drive API в /data/inbox/<lecture_id>.<ext>
n8n (HTTP Request, credential "psy Whisper token")
  └─ POST /extract-audio  {input_path, output_path, delete_input: true}  → /data/audio/<lecture_id>.16k.opus (~5 МБ)
  └─ POST /transcribe     {audio_path, initial_prompt}                    → текст и сегменты JSON
```

- В HTTP Request n8n выбирается Authentication → Predefined Credential Type → `Google Drive OAuth2 API` (credential `Google Drive account`, id `S7mhg7CBYGpInlHx`). n8n сам подставляет свежий access token в заголовок `Authorization` и обновляет его по ответу `401`. Сервис токен не хранит и не логирует.
- Через n8n идут только JSON-метаданные. Видео в памяти n8n не бывает.
- `/drive/fetch` пишет во временный `.part`, сверяет размер и `md5Checksum` Drive, проверяет свободное место (`size + WHISPER_MIN_FREE_MB`, по умолчанию 3 ГБ запаса; иначе `507`).
- После успешного opus видео удаляется (`delete_input: true`). Копия остаётся в Drive.
- `/drive/list-media` отдаёт медиафайлы папки курса на глубину `depth` подпапок (по умолчанию 1: папка курса → папки «Лекция NN» → файлы). Пагинация и фильтр `video/*`, `audio/*` — внутри сервиса.

### Почему Drive-эндпоинты без `WHISPER_TOKEN`

Заголовок `Authorization` занят токеном Google, а у HTTP Request в n8n только один credential. Положить `WHISPER_TOKEN` в обычный заголовок значит записать секрет в JSON workflow. Поэтому `/drive/*` проверяют только токен Google: без валидного токена Drive отвечает `401`, и сервис ничего не делает. Остальные POST (`/extract-audio`, `/transcribe`, `/files/delete`) требуют `WHISPER_TOKEN` (`Authorization: Bearer` или `X-Whisper-Token`). Порт наружу не опубликован, в сети `myn8n-n8n-iucjks` только n8n и traefik (`exposedByDefault: false`).

### Что проверить на первом прогоне (не проверено, workflow ещё нет)

1. n8n 2.35 отправляет токен predefined credential на хост `psy-whisper`, а не только на `googleapis.com`. Если у credential включено ограничение доменов (Allowed HTTP Request Domains), добавить `psy-whisper`.
2. У credential `Google Drive account` есть доступ на чтение к папке курса `1FwTgobJc50F4vTe0dxNOaZZTw7QdGnJP` (это может быть другой аккаунт Google).

### Запасной вариант, если пункт 1 не работает

Сервисный аккаунт Google с доступом «читатель» к папке курса. JSON ключа кладётся на сервер в `/opt/psy-whisper/` (chmod 600, вне git), сервис берёт токен сам. Для этого нужен владелец: создать сервисный аккаунт в Google Cloud и расшарить ему папку. Код под это пока не написан.

## Таймауты и длительность

| Шаг | Ориентир | Таймаут HTTP в n8n |
|---|---|---|
| `/drive/fetch` 3 ГБ | 2–10 мин в зависимости от канала | 60 мин |
| `/extract-audio` | 1–3 мин на часовое видео | 60 мин |
| `/transcribe` turbo int8, 8 CPU | ~0.15× длительности + загрузка модели | 180 мин |

Сервис однопоточный: второй тяжёлый запрос во время первого получает `409 busy`. Workflow на `409` возвращает job в `queued`, не в `failed`.

## Память

Контейнер живёт постоянно (`restart: unless-stopped`, `WHISPER_IDLE_EXIT_SEC=0`). Через `WHISPER_IDLE_UNLOAD_SEC=900` секунд простоя модель выгружается из RAM, процесс остаётся (~35 МБ). Лимит контейнера 4 ГБ. Тяжёлую модель Ollama (qwen 27B) во время распознавания не держать в памяти.

## Замеры на сервере (2026-11-10, синтетика 10.5 с речи, не курс)

| Состояние | RAM контейнера |
|---|---|
| Простой, модель не загружена | ~35 МБ |
| Пик первого `/transcribe` (скачивание и загрузка turbo) | ~2.0 ГБ |
| Модель в памяти после запроса | ~1.6 ГБ |

- Первый запрос: 244 с на скачивание весов (1.6 ГБ в volume `psy-whisper-models`) и загрузку, 11 с распознавание.
- Повторный запрос с тёплой моделью: 11.4 с на 10.5 с аудио. На коротком файле доминирует окно Whisper 30 с, коэффициент для часовой лекции на этом не считается. Перед прогоном настоящей лекции замерить на 2–5 минутах синтетики.
- `/extract-audio` mp4 → opus: 0.4 с. Распознанный текст совпал с синтезированной фразой.
