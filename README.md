# Тренажёр памяти для курса психологии

**Статус:** фаза 1 собирается частями. В репозитории уже есть сервис распознавания, расчёт FSRS и промпты вопросов. Workflow n8n и миграция базы — отдельные шаги.

Продукт помогает Наталье учить видеокурс психологии. Лекция проходит цепочку: транскрипт (свой faster-whisper) → структурированный конспект (дешёвая LLM) → термины и жизненные вопросы с выбором ответа → интервальные повторения FSRS в Telegram-боте. Оркестрация — n8n, данные — Postgres/Supabase с pgvector, площадка — Dokploy. Потолок расходов на работу продукта — 500 ₽ в месяц.

## Как начать

1. Прочитайте [AGENTS.md](AGENTS.md) и [план](docs/plan.md). Исходный контекст MVP — в [docs/kickoff_prompt.md](docs/kickoff_prompt.md).
2. Материалы курса лежат в [папке Google Drive](https://drive.google.com/drive/folders/1FwTgobJc50F4vTe0dxNOaZZTw7QdGnJP). Репозиторий публичный, поэтому видео, аудио, транскрипты и конспекты лекций остаются в этой папке и в локальных каталогах `data/` и `tests/fixtures/private/`.
3. Скопируйте `.env.example` в локальный `.env` и заполните значения у себя. Секреты остаются в `.env` и в credentials n8n.
4. Проверка уже лежащего кода: `node --test fsrs/fsrs.test.js prompts/prompts.test.js` и pytest из [services/whisper/README.md](services/whisper/README.md). Workflow и миграции на проде по-прежнему ждут своего подтверждения.

## Состав репозитория

| Путь | Назначение |
|---|---|
| [docs/](docs/README.md) | План реализации и kickoff |
| [prompts/](prompts/README.md) | Системные промпты LLM |
| [workflows/](workflows/README.md) | JSON-экспорт n8n и контракты узлов |
| [db/migrations/](db/migrations/README.md) | Миграции Postgres |
| [services/whisper/](services/whisper/README.md) | HTTP-сервис faster-whisper |
| [bot/](bot/README.md) | Telegram-бот |
| [fsrs/](fsrs/README.md) | Чистый расчёт интервалов FSRS |
| [tests/fixtures/](tests/fixtures/README.md) | Фикстуры регрессии без текстов курса |

## Бюджет

Не больше 500 ₽ в месяц на работу продукта. Распознавание речи — на своём сервере. Для текста — дешёвая модель, каждый вызов с записью токенов. Perplexity к этому продукту не подключается.
