# Запуск на удалённом GPU-сервере (Brev / любой Linux с NVIDIA)

Статус: **NOT_VERIFIED** — на Brev не запускалось. Всё остальное проверено на Windows + RTX 3060.

## Важно про данные
Модели работают на **вашем** арендованном сервере, а не во внешнем AI API. Это self-hosted, но **не локально**:
записи и расшифровки уходят в облако провайдера GPU. Для тестовых и синтетических записей это допустимо.
Для реальных совещаний нужно согласие заказчика. Не называйте такую схему «локальной» или «air-gapped».
Не используйте hosted inference API провайдера (NIM API и подобные): только свои модели на своей машине.
Ссылки-приглашения в организацию Brev дают доступ администратора. Не публикуйте их в Git и чатах;
если ссылка уже разослана, перевыпустите её.

## 1. Сервер (Ubuntu, GPU ≥ 24 ГБ VRAM)
```bash
git clone https://github.com/BAITC-Hacks/hack-8a4ed672-ub.git && cd hack-8a4ed672-ub
git checkout feature/hattama-live
python3.11 -m pip install --user uv
python3.11 tasks.py setup --asr
# CTranslate2 на Linux нужны cuBLAS 12 и cuDNN 9 (официальный способ faster-whisper для Linux):
.venv/bin/pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
export LD_LIBRARY_PATH=$(.venv/bin/python -c 'import os,nvidia.cublas.lib,nvidia.cudnn.lib;print(os.path.dirname(nvidia.cublas.lib.__file__)+":"+os.path.dirname(nvidia.cudnn.lib.__file__))')
cp .env.example .env && sed -i 's/^HATTAMA_PROFILE=.*/HATTAMA_PROFILE=gpu-server/' .env
python3.11 tasks.py models-prepare --only whisper-large-v3-turbo-ct2 whisper-large-v3-ct2 qwen3-14b-q4_k_m
python3.11 tasks.py doctor --asr-test
```

## 2. LLM (llama.cpp в Docker, слушает только localhost сервера)
```bash
docker run -d --name hattama-llm --gpus all -p 127.0.0.1:8081:8080 \
  -v "$HOME/.local/share/hattama/models/qwen3-14b-gguf:/models:ro" \
  ghcr.io/ggml-org/llama.cpp:server-cuda-b11120 \
  -m /models/Qwen3-14B-Q4_K_M.gguf --host 0.0.0.0 --port 8080 -c 8192 -np 1 -ngl 99 --jinja --no-webui
python3.11 tasks.py create-user --email sec@example.kz --name "Секретарь" --role secretary
python3.11 tasks.py dev --no-llm
```

## 3. Доступ с ноутбуков команды (8 ГБ RAM достаточно): SSH-туннель, без открытых портов
```bash
brev port-forward <имя-инстанса> --port 8000:8000     # или: ssh -N -L 8000:localhost:8000 <brev-host>
```
Дальше всё как локально: интерфейс по адресу http://localhost:8000, в расширении сервер `http://localhost:8000`.
`localhost` считается защищённым контекстом, поэтому HTTPS не нужен. Порт 8000 не открывайте в интернет.

## Вариант «всё локально, только LLM на сервере»
На ноутбуке API и ASR работают локально, а LLM доступна через туннель:
`ssh -N -L 8081:localhost:8081 <brev-host>`. Адрес `HATTAMA_LLM_BASE_URL=http://127.0.0.1:8081` не меняется,
список разрешённых хостов тоже. В облако уходят только тексты расшифровок, без аудио.

## Что проверить на сервере
`python3.11 tasks.py test`, `test-integration`, `smoke-local --with-models`, затем `scripts/stream_wav.py` с тестовыми
записями. Сравните поручения Qwen3-14B и Qwen3-4B на одинаковых записях и запишите результат в STATUS.md.
