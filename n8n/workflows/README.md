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
| Идемпотентность | claim; upsert `lecture_texts` / `chunks` по ключам; enqueue `konspekt:<lecture_id>` `ON CONFLICT DO NOTHING` |
| Читает | `psy.jobs`, `psy.lectures`, `psy.glossary_terms` |
| Пишет | `psy.lecture_texts`, `psy.chunks`, `psy.lectures`, `psy.jobs` (в т.ч. job `konspekt`) |
| Успех | `transcribed`, chunks ordinal с 1; opus не удалять; job `konspekt` queued; `Fin — Kick psy_04` (wait=false, `onError: continueRegularOutput`) |
| Ошибка | как в psy_02 (без delete opus) |

`workflow_version` (`psy_03_transcribe_v2`) пишется в `psy.jobs.payload`. Таймаут transcribe: 180 мин.

## Общие правила psy_04 / psy_05

- Имена узлов `{Zone} — {Verb}`: `In` / `Run` / `Load` / `LLM` / `DB` / `Fin` / `Shared` / `NOTE`.
- Порядок: `Run — Init Constants` → `Load — Backfill` (добавить недостающие jobs) → `Run — Create Job` (claim `SKIP LOCKED`, `attempts < 3`, зависший `running` > 4 ч) → `Run — Has Job`.
- DeepSeek **только** через `Shared — DeepSeek*` (HTTP `POST https://api.deepseek.com/chat/completions`, credential `DeepSeek account`, тип `deepSeekApi`). Модель `deepseek-flash`, `thinking: {"type":"disabled"}`. Из `usage` берутся `prompt_tokens`, `completion_tokens`, `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`.
- Каждый вызов → строка `psy.llm_usage` (`prompt_version`, `workflow_version`, `job_id`, `step`, `prompt_cache_hit_tokens`, `cost_usd`).
- Цены (peak, USD за 1M токенов) лежат в `Run — Init Constants`: miss 0.3, hit 0.006, out 1.2. `monthly_budget_usd = 5`, остановка на 80 %, `max_cost_usd_per_lecture = 0.30`. Через `In — Execute` лимиты можно только **понизить** (`max_cost_usd_per_lecture`, `monthly_budget_usd`, для psy_04 ещё `konspekt_single_call_max_input_tokens`).
- Ошибка: `Fin — Fail Context` → `Fin — Fail Job`. `409` → `queued`, attempts−1. Иначе `queued`, на 3-й попытке job и lecture → `failed`. Исключения (job `failed` сразу, attempts=3, статус лекции **не меняется**): `budget_exceeded`, «cards exist», `empty_transcript`, `empty_konspekt` (LLM не вызывается).
- В SQL `queryReplacement` — массив; JSON передаётся строкой и кастуется `::jsonb`. `workflow_version` пишется в `psy.jobs.payload`.
- Зависит от миграции `0002_llm_versions_and_term_examples.sql` (колонки `llm_usage.*`, `glossary_terms.life_example`, `questions.ordinal_in_subtopic` и partial unique index). Триггеров по расписанию нет: workflow запускаются kick-ом предыдущего шага или вручную (`In — Manual` / backfill).

## psy_04_konspekt

| | |
|---|---|
| Триггеры | `In — Manual`, `In — Execute` (passthrough; опц. `konspekt_single_call_max_input_tokens`, `max_cost_usd_per_lecture`, `monthly_budget_usd` — только вниз) — **не активировать** |
| Credentials | `psy Supabase (psy_n8n)`, `DeepSeek account` |
| Промпты | `konspekt_v2` (single / map-часть), `konspekt_reduce_v1` (склейка); system prompt читается из `prompts/*.md` при сборке |
| Идемпотентность | job `konspekt:<lecture_id>`; `lecture_texts` upsert по `lecture_id`; enqueue `terms:<lecture_id>` `ON CONFLICT DO NOTHING` |
| Читает | `psy.jobs`, `psy.lectures`, `psy.lecture_texts`, `psy.chunks`, `psy.glossary_terms`, `psy.llm_usage` (траты за месяц и по лекции) |
| Пишет | `psy.lecture_texts.konspekt_md` + `llm_model`, `psy.lectures`, `psy.jobs`, `psy.llm_usage` |
| Backfill | `lectures.status = 'transcribed'` без job `konspekt:<id>` → queued |
| Успех | `konspekt_ready`, job `done` (`payload.konspekt_result`: mode, parts, prompt_versions, токены, cost_usd), job `terms` queued, `Fin — Kick psy_05` (wait=false) |
| Ошибка | общие правила; пустой транскрипт / `budget_exceeded` → job `failed`, лекция остаётся в прежнем статусе |

Режим: оценка входа `символов / 2` токенов. `<= konspekt_single_call_max_input_tokens` (80 000) — один вызов. Иначе map: части по ~50 % лимита с перекрытием 1–2 сегмента → `Shared — DeepSeek` на каждую часть → `LLM — Collect Parts` → `LLM — Reduce Prep` → `Shared — DeepSeek Reduce`. Бюджетный предохранитель (`LLM — Budget Guard`) считает прогноз до первого вызова.

## psy_05_terms_questions

| | |
|---|---|
| Триггеры | `In — Manual`, `In — Execute` (passthrough; опц. `max_cost_usd_per_lecture`, `monthly_budget_usd` — только вниз) — **не активировать** |
| Credentials | `psy Supabase (psy_n8n)`, `DeepSeek account` |
| Промпты | `extract_terms_v2`, `gen_questions_v3`; валидатор + `subtopic_questions.js` встроены в Code при сборке |
| Фрагменты | konspekt ≤ ~5.5k (секция ± сосед; miss-окно по позиции подтемы), transcript ≤ ~7k (хиты ±1; без chunks — нарезка transcript ~1k) |
| Термины | цепочка definition: подтема → лекция → курс; если пусто — `skip_reason=no_terms_available`, DeepSeek не вызывается (`DB — Note No Terms`) |
| Merge thin | только при совпавшей `##` и длине primary < 400; score 0 не сливает |
| Идемпотентность | job `terms:<lecture_id>`; `subtopics` по `(lecture_id, ordinal)`; `glossary_terms` по `(course_id, canonical)`; `questions` по `(subtopic_id, ordinal_in_subtopic) WHERE kind='life_mcq'` |
| Читает | `psy.jobs`, `psy.lectures`, `psy.lecture_texts`, `psy.chunks`, `psy.glossary_terms`, `psy.cards` (проверка), `psy.llm_usage` |
| Пишет | `psy.glossary_terms`, `psy.subtopics`, `psy.questions`, `psy.lectures`, `psy.jobs`, `psy.llm_usage` |
| Backfill | `lectures.status = 'konspekt_ready'` без job `terms:<id>` → queued |
| Успех | `questions_ready`, job `done`, `payload`: `terms_count`, `subtopics_count`, `questions_count`, `short_questions` (подтемы с `< 10` вопросов и фактическим числом), `llm_calls`, `cost_usd` |
| Ошибка | общие правила; `cards exist` / `empty_konspekt` / budget → job `failed`, лекция не меняется; 0 вопросов всего → job `failed` (`no_questions_generated`) |

Термины: определение из другой лекции не перезаписывается (перезапись только для той же лекции или при пустом определении); `aliases` объединяются; `lecture_id` ставится, если был `NULL`; `life_example` следует за принятым определением. Подтемы вне нового списка удаляются только если карточек нет.

Вопросы (цикл `Run — Loop Subtopics`, batch 1): фрагменты конспекта/транскрипта выбираются по терминам подтемы → `Shared — DeepSeek Questions` → валидатор → при `< 10` валидных **один** повтор (`Shared — DeepSeek Questions Retry`: исправление или добор с уже принятыми stem в списке «не повторять») → принять все валидные (частичный успех). Повтор пропускается, если не хватает бюджета. Подтема без терминов с определением пропускается (попадёт в `short_questions` с 0).
