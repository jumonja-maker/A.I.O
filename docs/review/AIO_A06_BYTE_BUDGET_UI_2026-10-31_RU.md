# A06 — квота legacy по байтам и явный пользовательский список

Дата: 31.10.2026 (Москва). База QA `e79d2c31326f36f8001dd3ff557b63822c96380c`; [CI #41127013624 SUCCESS](https://github.com/jumonja-maker/A.I.O/actions/runs/41127013624), Windows 1462 PASS + 7 N/A, Linux 1464 PASS + 5 N/A, native LO 26.8.2.2 / EXE / AIO QA gate PASS. Сохраняются Guard PR #92 и durable legacy PR #95.

## До и после

| Условие | До | После |
|---|---|---|
| Мало каталогов, но слишком много байтов | Ограничение числа roots не останавливает рост диска | Production legacy имеет квоту 8 GiB, явно настраиваемую `AIO_LEGACY_STORAGE_BYTES`; конструктор принимает положительный integer/None |
| Объявленная загрузка превышает остаток | Чтение body и запись на диск | HTTP 507 `LEGACY_STORAGE_BYTES` до MultipartUpload/body, gate освобождён, соединение закрыто |
| Растёт native output/profile | Нет legacy byte monitor | Кооперативная проверка до/после native и через cancellation polls не чаще 2 раз/с. Ошибка завершает только собственное дерево и не запускает следующие targets |
| ZIP дублирует native bytes | Новая копия может заполнить диск | До открытия ZIP проверяется консервативная оценка полного ZIP_STORED; во время/после записи и после journal контролируется фактический размер |
| Несохранённые/непроверенные/занятые файлы | Не входили в byte quota | Учитываются все regular file bytes legacy parent, включая inputs, profiles, journals, archives, unknown/unverified и roots другого владельца; они не вытесняются |
| Не удалось измерить место | Не определено | Fail closed `LEGACY_STORAGE_MEASUREMENT_FAILED`; symlink/reparse/special entries, неполный доступ и исчерпание inspection budget не дают ложный свободный объём |
| Список прежних результатов | Только authenticated API | В «Состояние → Результаты прежнего конвертера» доступен явный список по 20 записей, сохранение копии, отдельное подтверждённое удаление, предупреждения и последняя измеренная квота |
| Загрузка/сохранение копии | UI не подключён | Открытие загрузки не считается подтверждённой конечной копией. Native completion предлагает проверить копию; автоматического release после save нет |

## Контракт квоты

Квота считает **логические размеры regular files**, а не выделенные блоки, память LibreOffice или весь пользовательский диск. Durable parent измеряется целиком; без durable parent учитываются принадлежащие transport roots. Проверка не держит transport lock при filesystem I/O, inventory/status используют только кэш измерения. Scan ограничен 100 000 entries и глубиной 32; предел исчерпания — ошибка проверки, не нулевой объём.

Это **кооперативная admission/live квота, не жёсткий лимит файловой системы**. Native writers и другие процессы могут превысить бюджет между измерениями; измерение не блокирует чужую запись и не является межпроцессным резервированием. Она не гарантирует ненулевое свободное место системного диска. G03/G05 обязаны отдельно проверить поведение и стоимость на полном пользовательском корпусе/диске.

Существующие результаты доступны для чтения и явного сохранения, даже если восстановленное хранилище уже больше новой квоты. Новое задание отклоняется. Ошибки quota/measurement сохраняют native outputs как unverified без публикации нового ready; ни исходный документ, ни старые несохранённые результаты не удаляются. Физически не завершившаяся очистка не выдаёт свободные байты. Освобождение места подтверждается новым измерением при следующем admission, а не уменьшением счётчика по намерению удалить.

## Проверки и отрицательные свидетельства

**13 новых byte-budget регрессий**: подсчёт всех классов файлов, over-quota restore/readback, locked cleanup, чужой OS owner, corrupt crash journal, отказ ZIP до создания, настоящий растущий Python CLI child через production `ProcessTree` (не LibreOffice), независимый живой соседний процесс, неиспорченный source, отсутствие следующего native target, медленный/недоступный scan без блокировки inventory, links/reparse, inspection bound, validation, throttle/cancel, HTTP before-body 507 и config validation before socket bind. На Linux все PASS; отдельный hosted Windows обязан исполнить тот же real-process тест, без нового skip.

**Production UI DOM regression PASS**: компонент действительно монтируется в Guard State, не делает legacy запросы до явного открытия, 20-row pagination, partial error badge, text-only untrusted names, 40-warning bound, save без release, browser download без ложного saved, explicit discard confirmation, stale close/pagehide reply, повторное открытие без зависших кнопок. Это controlled HTTP/native-save DOM, не installed Windows.

Целевые локальные проверки: **42 legacy methods PASS**, **21 HTTP streaming PASS**, **91 audit tests PASS**, **32 queue tests PASS**, existing recovery-warning UI PASS. Полный промежуточный source run до добавления UI: **1482 discovered / 1474 PASS / 1 GUI FAIL / 7 skips**; из skips 5 Windows N/A и 2 opt-in GUI INCOMPLETE. GUI FAIL — отсутствующая `libXtst.so.6`, а не PASS. Hosted полный набор, native/EXE и общий gate на точном feature SHA и затем отдельно на merged QA SHA обязательны; результаты появятся в PR после завершения.

## Остаток

**A06 ЧАСТИЧНО:** byte quota и базовый legacy inventory/save/discard UI реализованы в этом пакете; source UI не заменяет системный DPI и установленную Windows-программу. Явный повтор дискового поиска busy/unverified roots и экспорт/спасение unverified files без журнала ещё не реализованы. Большой startup hash scan и стоимость quota scan требуют G05. При повреждённом journal bytes сохранены, но не повышаются до успешной конвертации.

G01–G05, исходный FULLTEST 200/5723/1079, A09 MAIN protection и полный native/vendor/reproducible release A10 остаются открытыми. Canonical bag обновлён. Google Drive не подключён; архив 00AIO не обновлён. MAIN, Release, protection и пользовательский installer не изменялись.
