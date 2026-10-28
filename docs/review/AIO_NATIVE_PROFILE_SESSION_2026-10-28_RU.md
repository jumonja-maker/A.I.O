# Узкое исправление native private profile и завершения сеанса

Статус: **PENDING NIKITA**. Это корректирующий PR после блокирующего integration run 40607773754 на source `6b61cb1413e753e687d1ff61749e1164069a3c1c` (tree `a9717652ec545c3a2b3f47c58f9b3b3aef8abf41`). Все 9 прямых all-sheet экспортов прошли; реальная очередь не получила модель из native preflight. Исходные результаты сохранены в PR #74, тест не ослаблен.

Диагностика 40610648806 на неизменном product source: профиль длиной 187 символов дал только fallback import proof без nativeImportMs, короткий профиль 62 — живой native UNO model с nativeImportMs. Это вывод о ветке выполнения из различий метаданных и исходников. Сам диагностический job завершился failure: файл extensions.pmap был занят после завершения контролирующего worker, поэтому TemporaryDirectory не смог его удалить. Этот run не объявляется успешной приёмкой.

Изменения кандидата:

- Windows UNO profile создаётся эксклюзивно в отдельном коротком temp-каталоге; исходники и выходы туда не переносятся. Остались те же macro/interaction/locale настройки, в том числе приватная en-US конфигурация DIF.
- Новый owner хранит identity и случайный token в sibling marker. Root/reparse/marker mismatch не разрешают удаление. Чужие файлы, saved outputs и originals не входят в cleanup. При неподтверждённой остановке private profile сохраняется.
- Windows ProcessTree явно завершает только собственный Job Object и проверяет его ActiveProcesses до закрытия handle; выход одного launcher/worker не приравнивается к выходу всех descendants. Parent reaping и KILL_ON_JOB_CLOSE сохраняются и при ошибке запроса. Глобальных taskkill/поиска чужих office нет.
- Реальная Windows unit-проверка держит независимый handle дочернего процесса, проверяет его завершение и сохранение независимого процесса. Native helper дополнительно держит два настоящих LO-сеанса, закрывает первый и повторно импортирует во втором; проверяет все owned PID handles, удаление профиля/owner и все ячейки четырёхлистового XLSX с ID/2400 формулами.
- Отдельный native invariant: внешний синтетический original изменяется по наблюдённому событию import, а очередь обязана конвертировать исходный uploaded snapshot; saved output читается независимо, оба внешних файла сохраняются.

Локальные unit/contract проверки пройдены; Windows-specific test локально skipped, не PASS. До завершения NIKITA с B28 48/48, B29 108/108, соседями и полным source/Qt/DOM/Excel suite никакой Windows PASS не заявляется. Затем потребуется новый совместный remaining-bag stress3. Main/QA, пользовательские испытания, FULLTEST и релиз не затрагивались.

Документация Win32 API: [TerminateJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject), [QueryInformationJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject), [BasicAccountingInformation](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information). Семантика проверяется на настоящем Windows runner; одна документация не считается нативной приёмкой.
