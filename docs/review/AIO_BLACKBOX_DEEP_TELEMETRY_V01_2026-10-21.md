# A.I.O ↔ BLACK.BOX — Deep Telemetry v0.1 (QA-only)

Дата: 2026-10-21. Это **наблюдатель**, а не механизм управления или проверки качества. Ветка основана на ещё не принятом Draft PR #14 (Guard v0.8.1). Слияние с MAIN и Windows-пакетирование запрещены до отдельной приёмки.

## Протокол и архитектура

Контракт взят из `jumonja-maker/BLACK.BOX`, ветка `feature/stress-recorder-v01-20261021`:
- `docs/EVENT_PROTOCOL.md`
- `blackbox/recorder.py`
- `blackbox/report.py`

A.I.O записывает **только локальный JSONL**. BLACK.BOX с `--events` только читает новые строки (от текущего конца файла при старте). Никаких HTTP-сервисов, токенов, содержимого, имён или полных путей пользовательских документов. Нет импортов BLACK.BOX в A.I.O.

Источник: `app_backend/blackbox_telemetry.py`.
- По умолчанию выключен. Включается **только** заданием полного пути `AIO_BLACKBOX_EVENTS` до запуска A.I.O.
- Один daemon-поток записи; очередь ограничена **8192** событиями. В потоках Guard/транспорта — только `put_nowait`; нет файловых операций внутри их блокировок.
- События пишутся в **новый** `.jsonl` (`x`), в явно указанной пользовательской диагностической директории, не внутри TEMP и не в хранилище Guard.
- Отказ записи, запрет доступа и переполнение не блокируют и не меняют конвертацию. При переполнении номера `seq` имеют наблюдаемые пропуски; писатель добавляет `telemetry_dropped` с `reason_code=QUEUE_OVERFLOW` и числом потерь в `duration_ms` (совместимый скаляр v0.1; **не продолжительность** в этом одном типе события).
- Границы протокола: `type, seq, timestamp, request_id, file_id, target, stage, status, duration_ms, reason_code, process_pid`. UTC timestamps, последовательный seq внутри процесса. Идентификаторы заданий и файлов перед записью преобразуются в *стабильные в рамках одного запуска* криптографические keyed-хэши. Полное имя, исходный путь, ошибки OS/LO, содержимое документов НЕ пишутся. `process_pid` намеренно не заявляется как PID LibreOffice, пока его нет в исходном callback; BLACK.BOX отдельно наблюдает дочерние процессы.
- Диагностический файл — **уникальный на запуск**: append/reuse недопустим во избежание ложной непрерывности seq. Если файл уже существует, A.I.O продолжает работу, но этот сеанс событий не пишет.

## Как включить под Windows (PowerShell)

Укажи фактические пути к установленной A.I.O и к BLACK.BOX:

```powershell
$dir = Join-Path $env:USERPROFILE "Documents\AIO-BlackBox"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$env:AIO_BLACKBOX_EVENTS = Join-Path $dir ("aio-events-{0}.jsonl" -f (Get-Date -Format "yyyyMMdd-HHmmss-fff"))
$proc = Start-Process -FilePath "D:\ПУТЬ_К_AIO\A.I.O.exe" -PassThru
Write-Host "PID:" $proc.Id "JSONL:" $env:AIO_BLACKBOX_EVENTS
```

Запусти BLACK.BOX **после открытия A.I.O, но ДО начала стресс-теста**. Можно в другом терминале, подставив показанные PID и путь:

```powershell
python "D:\ПУТЬ_К_BLACKBOX\blackbox_app.py" --pid 12345 --events "C:\Users\USER\Documents\AIO-BlackBox\aio-events-YYYYMMDD-HHMMSS-fff.jsonl"
```

`EventTail` BLACK.BOX начинает читать **с текущего конца файла**, поэтому ранее произведённые события до запуска BLACK.BOX в его отчёт не попадут (хотя останутся в JSONL). Файл можно позднее разбирать отдельно. Если использован GUI BLACK.BOX, укажи тот же JSONL в поле события.

Чтобы отключить, заверши A.I.O и запусти заново без переменной, например в новой PowerShell-сессии или командой `Remove-Item Env:AIO_BLACKBOX_EVENTS` перед стартом. В уже запущенном A.I.O диагностика динамически не отключается.

## События Guard

`ConverterGuard._record()` остаётся единой точкой регистрации состояния. Телеметрия НИКОГДА не определяет переходы и не влияет на исходный GuardTrace/Revision.

- `job_created`, `job_queued`, `job_started`, `job_finished`, `job_failed`;
- `task_queued`, `task_started`, **строго по состоянию** `task_succeeded`, `task_failed`, `task_cancelled`;
- `preflight_batch_started/finished`, `preflight_started/finished`, `route_selected`, `libreoffice_started`, `native_stage` (`import`/`export`), `result_created`;
- `archive_started/ready/failed`, `save_confirmed`, `cancel_requested`, `file_cancel_requested`, `job_recovered`, `results_retained`.

Постфактум `task_finished` не считается успешным: ему НЕ сопоставляется `task_succeeded`. Preflight-исключения маркируются как `task_failed` со статусом `skipped` и кодом причины для полной сверки запланированных операций. Успех только после реальной `result_ready` операции.

Для старого прямого пути `ConverterTransport` добавлен аналогичный наблюдатель; сам транспорт не меняется и не заменяется. `libreoffice_started` означает callback запуска дочернего процесса, а не доказательство удачного импорта/экспорта.

## Честные ограничения v0.1

1. События best-effort: crash A.I.O, принудительное завершение или отказ диска способны оборвать последнюю часть очереди. **Отсутствующее событие не доказывает потерю конвертации**.
2. Выход BLACK.BOX v0.1 сохраняет только известные поля; не рассчитывать на дополнительные ID, payload или диагностические тексты.
3. При переполнении записываются marker + gap, но в BLACK.BOX `report.py` нет независимого полного доказательства жизненного цикла при пропусках. Это **неполная телеметрия**, а не автоматическое подтверждение логического дефекта.
4. События не включают исходные полные пути, PID каждого LO, содержимое результата, трассировки Python, метрики CPU/RAM (их собирает BLACK.BOX).
5. Запись в один существующий файл вторым A.I.O не поддерживается; новый сеанс — новый путь.
6. Минимальный накладной расход лучше измерять при одинаковых реальных стресс-тестах с/без диагностики. Потенциальная потеря диагностических событий не должна замедлять основную очередь.
7. Контракт протестировать на NIKITA Windows + BLACK.BOX GUI/CLI, с реальным LibreOffice. Одни mock-тесты не подтверждают нативный процесс.
8. Телеметрия **не** устраняет ошибки F01–F12 родительского Draft PR #14 и не реализует Guard Registry. Они требуют отдельной приёмки.

## Проверки

`python -m unittest discover -s tests -p test_blackbox_telemetry.py -v`.

Также отдельный GitHub Actions `.github/workflows/blackbox-telemetry-qa.yml` делает compile и изолированные тесты. До успешного CI **не считать PR готовым к интеграции**.

Ничего не записывать в спасённый Drive-бэкап стресс-теста:
https://drive.google.com/drive/folders/1aao9ArWlaxi8WJOnmPjQ9xkQPaLOYxS8?usp=drive_link
