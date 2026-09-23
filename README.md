# Хаттама Live — локальное автопротоколирование совещаний

Живой захват звука встречи (Google Meet / Teams Web / Zoom Web через расширение Chrome, или микрофон компьютера),
распознавание RU / KZ / смешанной речи **во время встречи**, финальный проход после неё, извлечение поручений
(действие, исполнитель, срок с основаниями), проверка секретарём, утверждение и экспорт в PDF/DOCX.
**Все модели работают локально**: записи, тексты и промпты не отправляются во внешние AI API.

> Статус: рабочий прототип. Что проверено и что нет — в [STATUS.md](STATUS.md). Не production-ready.

## Архитектура (кратко)

```
Chrome + расширение (tabCapture → offscreen → AudioWorklet)  ─┐   протокол hattama.audio.v1 (WebSocket,
Веб-панель (локальный микрофон)                              ─┼─▶ бинарные PCM-кадры, ACK после fsync)
                                                              │
FastAPI (apps/api/hattama) ── SQLite/PostgreSQL ── хранилище PCM (по позиции, без потерь)
   ├─ ASR-воркер: faster-whisper large-v3-turbo (live rolling windows + финальный проход)
   ├─ pipeline-воркер: говорящие → извлечение событий (LLM) → связывание → проверка оснований → вопросы
   └─ llama.cpp server (Qwen3-4B GGUF, только 127.0.0.1)
```

| Путь | Что там |
|---|---|
| `packages/contracts/` | протокол аудиоприёма v1 (spec, Python-кодек, тест-векторы) |
| `packages/audio-client/` | браузерный кодек, AudioWorklet, загрузчик с буфером и переподключением |
| `apps/api/hattama/` | API, приём, ASR, извлечение поручений, сроки, экспорт, уведомления |
| `apps/extension/` | расширение Chrome MV3 «Хаттама Live Companion» |
| `apps/web/static/` | веб-панель (без сборки и CDN) |
| `model-configs/` | манифест моделей (ревизии, sha256), профили ресурсов |
| `tests/` | unit, integration (WebSocket/БД), тест с реальной моделью |

## Быстрый старт (Windows / Linux, Python 3.11, Node 20)

```bash
python -m pip install --user uv
python tasks.py setup --gpu          # или --asr (без NVIDIA GPU)
python tasks.py doctor               # оборудование, рекомендуемый профиль
python tasks.py models-prepare       # whisper turbo (1.6 ГБ) + Qwen3-4B Q4_K_M (2.5 ГБ), sha256 проверяется
python tasks.py runtimes-prepare     # только Windows: llama.cpp b11120 CUDA 12.4
copy .env.example .env               # Linux: cp; при необходимости поправьте профиль
python tasks.py create-user --email sec@example.kz --name "Секретарь" --role secretary
python tasks.py dev                  # API + воркеры + LLM; интерфейс: http://localhost:8000
```

Подготовка (скачивание) и запуск — разные команды: `dev`, `test`, `smoke-local` ничего не скачивают.
Путь к репозиторию с кириллицей работает: `tasks.py` сам выставляет `PYTHONUTF8` и `PYTHONPATH`.
**Не храните данные и модели в OneDrive**: по умолчанию они лежат в `%LOCALAPPDATA%\Hattama`.

### Расширение Chrome
```bash
python tasks.py build-extension      # → apps/extension/dist
```
`chrome://extensions` → «Режим разработчика» → «Загрузить распакованное» → `apps/extension/dist`.
Настройка микрофона: страница «Параметры» расширения (разрешение, выбор устройства, проверка уровня).

### Текущий веб-MVP

Панель обновлена по макету: **Встречи**, **Протокол**, **Поручения**. Вход, поиск, создание встреч, проверка протокола, смена статусов поручений и экспорт подключены к API. Экран записи и отдельные телефонные экраны отложены; инструкция записи ниже описывает прежнюю панель и сохранённые возможности бэкенда. Подробнее — [apps/web/README.md](apps/web/README.md).

### Как провести встречу
1. Веб-панель → «Новая встреча»: дата, часовой пояс (по умолчанию Asia/Almaty), участники.
2. Уведомите участников и подтвердите это в панели. Без подтверждения запись не начнётся.
3. «Расширение Chrome: получить код» → во вкладке Meet/Teams Web/Zoom Web откройте значок расширения,
   введите `http://localhost:8000` и код → «Начать запись этой вкладки».
   Для очной встречи нажмите «Микрофон этого компьютера».
4. Микрофон в расширении по умолчанию **выключен**. Он пишется независимо от mute в Meet/Teams/Zoom,
   поэтому включайте и выключайте его кнопкой расширения. Используйте наушники.
5. «Остановить запись» → финальный проход → статус NEEDS_REVIEW → проверьте поручения и вопросы →
   «Утвердить» → PDF/DOCX. Неутверждённый протокол помечается «ЧЕРНОВИК».

Настольные клиенты Zoom/Teams **не поддерживаются** (только веб-клиенты во вкладке Chrome).
Бот-участник (meeting-agent) не реализован.

## Команда с 8 ГБ RAM

Замеры на этой машине: `turbo int8_float16` на GPU занимает ≈0.9–1.15 ГиБ VRAM и 0.65 ГиБ RAM, RTF 0.05.
На CPU `int8` RAM ≈1.2 ГиБ (пик 1.56 ГиБ при загрузке), RTF 0.44 за один проход.
Qwen3-4B Q4_K_M занимает ≈3.2 ГиБ (веса + KV-кэш на 4096 токенов).
При 8 ГБ RAM ОС и Chrome с открытой встречей уже занимают 4–5 ГБ, поэтому ASR и LLM одновременно не помещаются.

**Рекомендуемый вариант: один сервер на команду, остальные — тонкие клиенты.**
- Сервер (`tasks.py dev`) запускается на одной машине с NVIDIA GPU ≥6 ГБ или с ≥16 ГБ RAM.
- Коллегам с 8 ГБ нужен только Chrome с расширением: оно передаёт звук вкладки на сервер
  и почти не расходует память.
- На сервере добавьте его адрес в `HATTAMA_ALLOWED_ORIGINS`. Для работы по сети нужен HTTPS/WSS
  (браузер требует защищённый контекст). Этот сценарий **не проверен** — см. STATUS.md.

**Если сервер обязательно запускать на ноутбуке с 8 ГБ без GPU:**
1. В `.env` укажите `HATTAMA_PROFILE=cpu-8gb`. Live-распознавание пойдёт на CPU с задержкой, не в real-time,
   а LLM во время встречи не запускается.
2. Во время встречи: `python tasks.py dev --no-llm`.
3. После встречи остановите `dev` (это освобождает ≈1.5 ГБ RAM ASR-воркера) и запустите
   `python tasks.py llm` и `python tasks.py dev --no-asr`. Анализ поручений выполнится из очереди.
4. Закройте лишние вкладки и приложения. Swap спасает от падения, но делает работу очень медленной.

## Тесты
```bash
python tasks.py test                 # unit (Python) + кодек JS против общих тест-векторов
python tasks.py test-integration     # API, WebSocket-протокол, БД (без моделей)
python tasks.py smoke-local --with-models   # полный маршрут с реальными моделями, внешняя сеть заблокирована
```

## Лицензии моделей
Whisper large-v3-turbo (CT2, `dropbox-dash/faster-whisper-large-v3-turbo`): MIT.
Qwen3-4B-GGUF: Apache-2.0. llama.cpp: MIT. Шрифт DejaVu: лицензия в `apps/api/hattama/export/fonts`.
pyannote community-1 — gated-модель: условия принимает сам пользователь на Hugging Face.
Ревизии и sha256 закреплены в `model-configs/model-manifest.json`.
# hack-8a4ed672-ub
Hackathon team repository for UB

There are problems occured. Can you wait a little bit more for the main codebase.
