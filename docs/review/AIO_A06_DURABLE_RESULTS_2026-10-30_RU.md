# A06 — опубликованные результаты legacy API переживают закрытие и restart

Дата: 30.10.2026. База `e29a19482f0ad3564d66e7f0b7a260d9cb2ace55` (A10/Pillow 13.0.0 + уже проверенный M19). Отдельный небольшой пакет только в QA; Guard Recovery PR #92 не переписан.

## Причина и до/после

Контроль на неизменном `converter_transport.py` базового SHA подтвердил: успешный legacy result существовал до `close()`, а после закрытия исчез. `close()` вызывал `release()` для всех tickets, и TemporaryDirectory удалял единственную несохранённую копию. Recovery inventory существовал только в памяти.

| Сценарий | До | После |
|---|---|---|
| Штатное закрытие с опубликованным result | Единственная копия удалялась | Production server использует отдельный durable legacy workspace, close отпускает ownership, не удаляя результат |
| Авария после публикации / до finish | Нет durable inventory | Checksum journal + размер/SHA файла, новый приватный ticket после restart |
| Повреждённый journal / изменённый файл | Не восстанавливается после restart | Не получает ready; существующие bytes остаются, warning видим через legacy inventory |
| Конкурирующий экземпляр | Только in-memory ownership | OS owner lock, busy root не присваивается и не удаляется |
| Одинаковый request ID в двух journals | Не учтён | Ни один результат не выбирается автоматически; обе копии сохранены с диагностикой |
| Ошибка записи journal / нехватка места | Потеря при финальной очистке | Native outputs сохраняются как unverified, без ложного успеха |
| Явный release с ошибкой cleanup | Только память текущего процесса | Durable sibling retirement barrier не даёт воскресить удаляемый result |
| Последний HTTP chunk / немедленный release | Возможна гонка с file lease | Lease освобождён до завершающих bytes; для пустого файла — до headers |

## Контракт и границы

- Legacy использует `legacy-jobs/aio-legacy-*` и `legacy-result.json`, отдельно от Guard job files/protocol. Общим остаётся только уже тестированный низкоуровневый OS lock / identity-bound retirement primitive.
- Журнал создаётся и fsync-ится до публикации ticket. Содержит относительный output path, request ID, размер/SHA-256, исходный target report и format IDs. Session tokens и download tickets на диск не записываются; после restart ticket меняется.
- Пути проверяются внутри собственного root, symlink/reparse paths и traversal отвергаются. JSON bounded до 16 MiB; hashes считаются потоково. Ни оригинал пользователя, ни макросы не открываются для выполнения/записи.
- Restore подтверждает только существование/неизменность байтов ранее опубликованного результата. `restoredFromJournal=true`, `conversionQualityCompared=false`; partial target failures остаются failures.
- Утрата журнала/ошибка публикации сохраняет доступные native bytes, но **автоматическое salvage всех unverified outputs пока не реализовано**. Эти файлы не повышаются до готовой конвертации.
- Production `start_server` по умолчанию активирует durable legacy parent. Прямой `ConverterTransport()` без parent сохраняет прежний ephemeral контракт для изолированных интеграций/фикстур; это не используется как production restart evidence.
- Byte quota, подключение legacy inventory к пользовательскому UI, явный retry busy/unverified scan, полный installed-Windows crash и все исходные случаи ещё требуются. **A06 остаётся ЧАСТИЧНО**.

## Проверки

**15 новых детерминированных регрессий**: normal close/restart, настоящий child `os._exit(23)` без finish/close, corrupt journal, изменённые bytes, checksummed traversal, concurrent ownership, duplicate IDs, explicit release, locked retirement, disk-full journal, partial-success ZIP, zero-byte native text, headers/final chunk ordering и реальный HTTP server restart с новым session token. Native conversion — controlled fixture; это не установленное приложение и не нативная конвертация пользовательского корпуса.

**27 legacy tests PASS**, включая 12 прежних; **21 streaming HTTP tests PASS** (в том числе 105 MiB output без накопления файла в RAM); **11 transport audit + 9 code-audit tests PASS**. Полный source Windows/Linux/native/EXE/gate exact SHA обязателен отдельно.

Промежуточные неудачи не скрыты: при добавлении нового HTTP теста часть fixture-кода ошибочно оказалась в другом методе (NameError), положение исправлено. Дважды воспроизведена прежняя гонка release-after-download в неизменном строгом тесте; тест не ослаблен, введена проверяемая граница stream completion / lease release и два детерминированных теста. Отдельный bounded прогон прежнего HTTP case 10 раз был 10/10 PASS, но именно детерминированный ordering test подтверждает исправление.

Локальные GUI source-прогоны ограничены отсутствием libXtst.so.6; ошибки не превращаются в skips. Hosted результаты не объявляются до завершения. Настоящий Windows installer, системный DPI, USB/network и G01–G05 этим пакетом не проверены. MAIN/Release/protection не менялись. Google Drive не подключён, архив 00AIO не обновлён.
