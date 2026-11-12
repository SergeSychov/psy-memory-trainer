# workflows

Контракты и указатели на JSON-экспорт n8n. Канонические файлы: [`n8n/workflows/`](../n8n/workflows/) (+ `.id`). Деплой: `n8n/scripts/pull_workflow.py` / `push_workflow.py` (env из `~/.config/psy-memory-trainer/n8n.env`).

| Workflow | Статус | Назначение |
|---|---|---|
| `psy_01_ingest_cloud_folder` | в n8n, **inactive** | Drive list-media → lecture + job extract |
| `psy_02_extract_audio` | в n8n, **inactive** | claim → fetch → extract → audio_ready |
| `psy_03_transcribe` | в n8n, **inactive** (v2) | claim → glossary prompt → whisper → chunks → job `konspekt` + kick psy_04 |
| `psy_04_konspekt` | в n8n, **inactive** | claim `konspekt` → DeepSeek (single/map-reduce) → `konspekt_md` → job `terms` + kick psy_05 |
| `psy_05_terms_questions` | в n8n, **inactive** | claim `terms` → термины/подтемы → MCQ по подтемам (валидация, один добор) → `questions_ready` |
| `psy_06`…`psy_09` | позже | FSRS, Telegram |

Подробные контракты (триггеры, credentials по имени, идемпотентность, таблицы, успех/ошибка) — в [n8n/workflows/README.md](../n8n/workflows/README.md).

Перед коммитом JSON: без `pinData` / `staticData` / значений секретов; ссылки `{id,name}` на credentials допустимы.
