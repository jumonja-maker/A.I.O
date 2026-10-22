# A.I.O — отчёт по исправлениям F01–F13 (22.10.2026)

**Источник:** `AIO_CODE_AUDIT_2026-10-22_PR22.md` (глубокий аудит PR #22).  
**Ветка:** `fix/aio-code-audit-reliability-20261022`  
**PR:** https://github.com/jumonja-maker/A.I.O/pull/23  
**База PR:** `fix/aio-guard-bugbag-ui-status-encoding-20261022` (PR #22), **не `main`**.  
**Режим:** Draft / QA. Патч не выпускать и не объединять в `main` без тестов.

## Сводка на дату отчёта

Патч исправляет подтверждённые нарушения контроля памяти и успешности native-выходов, устраняет межзапросную гонку пропуска и уменьшает риск зависания GUI. Некоторые пункты аудита относятся к не измеренной утечке или требованию доказать *смысловую/визуальную эквивалентность*. Их нельзя считать закрытыми только изменением исходников.

| ID | Приоритет | Что сделано | Состояние |
|---|---|---|---|
| F01 | P1 | У HTMLParser введены ограничение накопленного незавершённого буфера (512 KiB) и объёма сканирования (32 MiB); при неопределённости preflight блокирует без ложного успеха | Код исправлен; runtime-проверка ожидается |
| F02 | P1 | Общая CLI/UNO-проверка основного результата целевого формата, ненулевой длины и сигнатуры для известных контейнеров; HTML sidecar не считается HTML | Код исправлен; native-совместимость требует проверки |
| F03 | P1 | Для Qt native-save введён ограниченный watchdog (30 минут), cleanup слушателя/таймера и безопасный retry по новому URL; отсутствие ACK не объявляется сохранением | Код исправлен; проверить Qt/WebEngine |
| F04 | P1 | `shutdown()` и `server_close()` вынесены из GUI thread в поток, завершение через Qt signal; несохранённые результаты не удаляются | Код исправлен; проверить закрытие во время долгого сохранения |
| F05 | P1/P2 | Статистика `maxJobs`, retained/unsaved jobs, ticket, RAM-independent spool bytes; остаётся только явно подтверждаемое освобождение | Частично: лимит 16 безопасно оставлен, нужен UX при заполнении |
| F06 | P2 | Папки без `job.json` и непригодные журналы перечисляются как orphan warnings; автоматического удаления нет | Частично: ручной экспорт/спасение orphan-файлов через GUI пока не реализован |
| F07 | P1/P2 | Preflight ловит `url(...)`, `@import`, CSS style и пути `/assets/...`; не обращается в сеть | Частично: CSS не является полным стандартным парсером |
| F08 | P2 | Проверка CSV честно помечает выбранные по sample данные `csvImportSampleOnly`, UI предупреждает о непроверенном хвосте | Частично: нормализованное постсравнение значений и типов не реализовано |
| F09 | P2 | В объединителе Excel source rows освобождаются по мере их копирования в новый workbook, вместо удержания до сериализации | Частично: нет профиля peak RSS/handles для 50 MiB и 1 млн ячеек |
| F10 | P2 | Backend сравнивает `expectedTaskId` с `currentTask` под одним RLock. UI отправляет ID, полученный после свежего GET | Код исправлен; регрессия межзапросной гонки добавлена |
| F11 | P2 | Результаты с недолговечным журналом получают `resultAvailability=ready_volatile`, `recoveryDurable=false`; пользовательский warning сохраняется | Код исправлен; проверить сбой fsync на Windows |
| F12 | P2 | Повторная очистка только **уже released** временных результатов, диагностика `cleanupPending`/retained/leases | Частично: прямой транспорт по-прежнему требует явного `/release` |
| F13 | P2 | `qualityCompared=false` выведено отдельно от факта native export; успешная проверка заголовка не выдаётся за проверку содержимого | Открыто: полноценная fidelity-матрица ещё не выполнена |

## Защита данных

- Не устанавливаются искусственные запреты на пары расширений или модели LibreOffice.
- Нельзя автоматически удалять **unsaved** tickets, jobs, recovery folders ради освобождения 16 слотов.
- Отсутствие Qt ACK, неверный primary, CSV sample, порча журнала и неполный HTML анализ **не** должны выглядеть как подтверждение безошибочной/без потерь конвертации.
- При `ready_volatile` исходные outputs могут открываться в текущем процессе, но **нельзя обещать** восстановление после аварийного завершения. Сохранить в выбранную пользователем папку как можно раньше.

## Добавленные тесты

- `tests/test_code_audit_regressions.py`: незавершённый HTML comment и bounded parser, CSS/root URI, HTML sidecar-only, пустой/неправильный PDF, atomic compare-and-skip, недолговечный ledger, orphan без манифеста, cleanup-retry direct transport.
- `tests/guard_native_save_watchdog.mjs`: тайм-аут native-save, cleanup listener/timer/DOM anchor, поздний ACK, повторное сохранение с другим URL.
- `tests/guard_save_queue_v09.mjs`: контролирует отправку `expectedTaskId`.
- `tests/test_guard_uno_pipeline_v12.py`: native PDF fixtures приведены к минимально валидной сигнатуре.
- `.github/workflows/clean-foundation-tests.yml`: включены новые Python и JS gates.

### Команды для чистого checkout

```bash
python -m compileall -q app_backend launcher.py
node --check src/bridge.js
node --check src/converter-bridge.js
node --check src/guard-queue.js
python -m unittest discover -s tests -p 'test_code_audit_regressions.py' -v
node tests/guard_native_save_watchdog.mjs
node tests/guard_save_queue_v09.mjs
python -m unittest discover -s tests -p 'test_excel.py' -v
python -m unittest discover -s tests -p 'test_guard_uno_pipeline_v12.py' -v
```

**Состояние выполнения на момент составления отчёта:** исходники/регрессии коммитированы; успешное исполнение всего набора **не подтверждено**. Hosted GitHub checks ранее показывали `failure` с **нулём шагов** (логи недоступны), то есть не проверяли ни Python, ни Node. Сделан повторный запуск двух неудачных workflows, но до появления реальных step logs не засчитывать результат.

## Нативная приёмка, обязательная перед слиянием

1. Установить Windows QA сборку с этим HEAD поверх PR #22. Прогнать Qt/WebEngine сохранение в файл и папку, закрытие во время долгой записи и потерю save-callback; проверить отзывчивость окон, фоновый shutdown и сохранность outputs.
2. На том же LibreOffice Windows 26.8.1.1 повторить точечные ODC standalone/embedded, ODT level3→CSV/SVG/EPUB, DOCX и Draw→HTML. До того не утверждать, что спорные направления корректны.
3. Запустить FULLTEST только по явной команде: baseline corpus SHA, осмотр output манифестов, сравнение страниц PDF, raster dimensions/pixels, HTML/CSS assets, normalized CSV rows/cell types, formula/chart preservation, EPUB structure.
4. Собирать RSS/handles отдельно для AIO, WebEngine, UNO python, soffice; сравнить baseline и этот HEAD на одинаковом маршруте/данных. Проверить отсутствие роста после save/release/close, а не только Python tracemalloc.
5. Оценить отдельным патчем безопасный просмотр и экспорт **orphan** outputs, UX `JOB_LIMIT`, варианты memory-spool Excel и coverage fidelity; эти части F05/F06/F08/F09/F12/F13 не выдавать за закрытые.

**Итог:** PR #23 пригоден как QA-кандидат на устранение нескольких P1, но **не является доказательством, что все 13 пунктов полностью устранены**. `main` не менялась, массовая конвертация и установщик не запускались этим патчем.
