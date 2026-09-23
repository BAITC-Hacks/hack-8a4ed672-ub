# Протокол аудиоприёма `hattama.audio.v1`

Транспорт: WebSocket `/api/v1/ingest/ws` (WSS в локальной сети, WS допустим только для `localhost`).
Текстовые сообщения — JSON (схема: `schemas/ingest-messages.schema.json`), аудио — бинарные кадры ниже.

## Рукопожатие

1. Сервер проверяет `Origin` по списку разрешённых (веб-интерфейс, ID расширения, бот). Неизвестный Origin → закрытие 4403.
2. Клиент первым сообщением отправляет `hello`. Секрет передаётся **только в теле сообщения**, не в query-string:
   * расширение/бот — `token` (короткоживущий, получен через pairing, привязан к одной capture-сессии);
   * веб-интерфейс — HttpOnly cookie сессии + `capture_session_id` (проверяются права секретаря на встречу).
3. Сервер отвечает `welcome` (лимиты, уровень ACK) либо `error` + закрытие.
4. Для каждого источника клиент отправляет `source_open` (source_id, kind, sample_rate, channel_count, capture_epoch,
   epoch_start_wall_us). Сервер отвечает `source_ready` c `source_index` и `resume_from_sequence`.

`session_id` связан с соединением на шаге 2; `source_index` в кадре связывает кадр с (session, source_id, epoch).

## Бинарный кадр (little-endian, заголовок 48 байт, без выравнивающих вставок)

| Смещение | Размер | Тип | Поле | Ограничения |
|---:|---:|---|---|---|
| 0 | 4 | bytes | magic | `HTA1` |
| 4 | 1 | u8 | version | `1` |
| 5 | 1 | u8 | kind | `1` = PCM s16le interleaved |
| 6 | 2 | u16 | header_length | `48` (payload начинается с этого смещения) |
| 8 | 2 | u16 | source_index | выдан сервером в `source_ready` |
| 10 | 2 | u16 | channel_count | 1 или 2, равен объявленному в `source_open` |
| 12 | 4 | u32 | capture_epoch | равен объявленному в `source_open` |
| 16 | 8 | u64 | sequence_number | 0,1,2… без пропусков внутри epoch |
| 24 | 4 | u32 | sample_rate | равен объявленному; 8000…96000 |
| 28 | 4 | u32 | sample_count | кадров на канал, 1 … sample_rate×2 |
| 32 | 8 | i64 | capture_timestamp_us | wall-clock (Unix, мкс) первого сэмпла по часам клиента |
| 40 | 8 | u64 | start_sample | индекс первого сэмпла от начала epoch |
| 48 | N | bytes | payload | N = sample_count × channel_count × 2 |

Максимальный размер кадра — `limits.max_frame_bytes` из `welcome` (по умолчанию 524 288 байт).
Нарушение формата → `error{code:"bad_frame"}`; повторное нарушение → закрытие 4400.

## Временная шкала

* Позиция сэмпла внутри epoch задаётся `start_sample` (часы звуковой карты), а не временем прихода.
* `capture_timestamp_us` каждого кадра сохраняется как якорь (sample_index → wall-clock) для учёта дрейфа часов.
* Шкала сессии: `t_ms = (epoch_start_wall_us − timeline_origin_us)/1000 + start_sample/sample_rate×1000`
  с поправкой по якорям. Источники одной capture-сессии из одного AudioContext имеют общие часы.
* Ресэмплинг в 16 кГц выполняется на сервере потоковым фильтром с сохранением состояния (`hattama.audio.resample`).

## Подтверждение (ACK) и сохранность

* `ack{source_index, capture_epoch, durable_sequence, durable_sample}` — **кумулятивный**: все кадры с
  `sequence ≤ durable_sequence` записаны в файл epoch и выполнен `fsync` (уровень `ack_level:"fsync"`).
  Получение в оперативную память ACK не порождает.
* Клиент хранит неподтверждённые кадры в ограниченном буфере и после переподключения повторяет с
  `resume_from_sequence`. Повтор кадра идемпотентен (запись по смещению `start_sample`).
* Переполнение буфера клиента → клиент отбрасывает старейшие кадры и отправляет `gap_report`. Сервер
  фиксирует разрыв (`AudioGap`), заполняет интервал тишиной на шкале и показывает разрыв в интерфейсе.
* Пропуск sequence без `gap_report`: сервер держит до 64 кадров вне порядка; если пропуск не заполнен за
  2 с — фиксирует разрыв `sequence_gap`. Поздно пришедший кадр перезаписывает тишину и сужает разрыв.

## Остановка

`stop_requested` (сервер) → клиент останавливает треки, досылает кадры, отправляет `source_close{final_sequence}`
по каждому источнику → сервер делает fsync, отвечает `source_closed` → после закрытия всех источников сессия
переходит в `STOPPED`, финализация ставится в очередь **один раз** (уникальный ключ задания).
Если клиент не закрыл источники за `stop_grace_s`, сервер закрывает их сам с причиной `stop_timeout`.

## Версионирование

Несовместимое изменение формата — новый `version` в заголовке и новый `protocol` в `hello`.
Тестовые векторы: `test-vectors/audio-frame-v1.json` (проверяются Python- и TypeScript-кодеками).
