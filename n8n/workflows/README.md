# n8n workflows (psy)

JSON-экспорт и `.id` файлы. Деплой: `python3 n8n/scripts/pull_workflow.py <slug>` / `push_workflow.py <slug>`. Env: `~/.config/psy-memory-trainer/n8n.env` (`N8N_API_URL`, `N8N_API_KEY`). Секреты в git не класть. `pinData` / `staticData` в экспорте быть не должно.

Сводка по плану также в [workflows/README.md](../../workflows/README.md).

## psy_01_ingest_cloud_folder

| | |
|---|---|
| Триггеры | `In — Schedule` (*/15), `In — Manual`, `In — Execute` (`course_id`) — **не активировать** |
| Credentials | `psy Supabase (psy_n8n)`, `Google Drive account` |
| Идемпотентность | `lectures (course_id, external_file_id)`; jobs `extract_audio:<lecture_id>` |
| Читает | `psy.courses` |
| Пишет | `psy.lectures`, `psy.jobs` |
| Успех | новая лекция `queued` + job `extract_audio` queued; kick psy_02 (wait=false) |
| Ошибка | Drive list error → ветка error HTTP (без failed lecture) |

Правило: элементы `folder_path`, начинающиеся с `_`, пропускаются.

## psy_02_extract_audio

| | |
|---|---|
| Триггеры | `In — Manual`, `In — Execute` |
| Credentials | `psy Supabase (psy_n8n)`, `Google Drive account`, `psy Whisper token` |
| Идемпотентность | claim `SKIP LOCKED`; enqueue `transcribe:<lecture_id>` |
| Читает | `psy.jobs`, health whisper |
| Пишет | `psy.jobs`, `psy.lectures` |
| Успех | `audio_ready`, opus в `/data/audio`, job transcribe queued, kick psy_03 |
| Ошибка | `409` → queued (attempts−1); иначе retry queued / после 3 → job+lecture `failed`; cleanup inbox |

Таймауты HTTP: fetch/extract 60 мин.

## psy_03_transcribe

| | |
|---|---|
| Триггеры | `In — Manual`, `In — Execute` |
| Credentials | `psy Supabase (psy_n8n)`, `psy Whisper token` |
| Идемпотентность | claim; upsert `lecture_texts` / `chunks` по ключам |
| Читает | `psy.jobs`, `psy.lectures`, `psy.glossary_terms` |
| Пишет | `psy.lecture_texts`, `psy.chunks`, `psy.lectures`, `psy.jobs` |
| Успех | `transcribed`, chunks ordinal с 1; opus не удалять |
| Ошибка | как в psy_02 (без delete opus) |

`workflow_version` пишется в `psy.jobs.payload`. Таймаут transcribe: 180 мин.
