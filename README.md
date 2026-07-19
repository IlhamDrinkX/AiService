# AI Service System

Локальный AI-координатор сервис-деска: собирает данные из Discord, корп. почты, Google Drive, Notion, самописного трекера задач и сервис-деска, парсит фото, складывает всё в базу знаний и работает координатором заявок. Обработка через ИИ — только через Router AI (например OpenRouter), без локальных моделей.

Полная архитектура и дорожная карта — в [architecture_plan.md](architecture_plan.md).
Текущий статус и следующие шаги — в [PROGRESS.md](PROGRESS.md).

## Коннекторы

| Коннектор | Статус | Папка |
|---|---|---|
| Discord | работает | `connectors/discord/` |
| Gmail | работает | `connectors/gmail/` |
| Google Drive | работает | `connectors/drive/` |
| Tracker (DrinkX) | работает | `connectors/tracker/` |
| Service Desk (DrinkX) | работает, read+write | `connectors/servicedesk/` |
| Fibbee ERP | работает, read+write | `connectors/fibbee/` |
| Notion | заблокирован (нужны права Member/Admin) | — |

Подробности и что дальше — см. [PROGRESS.md](PROGRESS.md). У каждого коннектора свой README с шагами настройки и запуска.

## Сервисные модули (`core/`)

| Модуль | Статус | Папка |
|---|---|---|
| Router AI (единая точка выхода в OpenRouter) | работает | `core/router_ai/` |
| Сводные отчёты по заявкам/тикетам/задачам | работает (MVP) | `core/reporting/` |
| Веб-интерфейс (тёмная тема, FastAPI+HTMX) | работает (MVP) | `core/webapp/` |

Веб-интерфейс — единая точка входа для всего: задачи, отчёты, база знаний, Discord +
мониторинг, локально на твоём ноутбуке (`http://127.0.0.1:9000`). Запуск — двойной
клик по [`start_webapp.bat`](start_webapp.bat). Что уже упрощено в v1 —
`core/webapp/README.md`. План — [functional_plan_ui.md](functional_plan_ui.md).

## Первый запуск / своя копия проекта

Секреты (токены, `credentials.json`, `.env`) и локальные базы (`data/`) не хранятся в git — у каждого пользователя копии проекта свои учётные данные. Как их завести — в [SETUP_NEW_USER.md](SETUP_NEW_USER.md).
