# A.I.O Guard v18 — независимый технический аудит перед возможным `main`

**Дата:** 2026-10-25. **Аудируемый QA-коммит:** `24f8e6bce2f91462ac8ffed25b92be7354c2bd16` (PR #40 + PR #41). **Текущий `main` при проверке:** `2bab2d0c31fe84a0d07404a33219dd5bdad834b7`. **Область:** source-code review, GitHub тестовые журналы, архитектурные и граничные сценарии. **Не сделано:** собственный полный Windows EXE/Setup/FULLTEST-510, нагрузочный GUI-run, визуальная экспертиза реальных файлов; source-code finding ≠ доказанная эксплуатация или фактическая порча реального документа.

## Общая оценка

**Вердикт: CONDITIONAL HOLD для `main`.** Текущую v18 сохраняем как рабочую QA-базу: выполненные регрессии подтверждают высокую функциональную готовность, но несколько неочевидных погрешностей требуют узких тестов или исправлений до признания `main` безопасной базой. Патчи ядра в ходе аудита не делались. Основные ограничения уже явно отражены в документации (отсутствие полного FULLTEST, GUI/Setup, true visual/semantic fidelity), поэтому их нельзя выдавать за новые обнаруженные runtime-сбои.

**Положительное свидетельство:** функциональные исправления v18 не ослабляют регистрацию native-маршрутов, качество ZIP/EMZ/WMZ и независимый SHA readback; batch source cache не переиспользует mutable UNO-модель. Финальная NIKITA QA [40138781539](https://github.com/jumonja-maker/A.I.O/actions/runs/40138781539): 635 найденных тестов, 628 PASS, 7 платформенно неприменимых POSIX, 0 FAIL/ERROR, native v16 8/8, legacy 11/11, v17 22/22. Локально для Linux в отчёте PR #41: 635/635 source PASS, 21/21 targeted native. Windows GUI и установленный бинарник **не выполнялись**.

## F-01 [P1] Непроверенный Host перед выдачей токена локальным HTTP-сервером

**Код:** [server.py #L160-L177](https://github.com/jumonja-maker/A.I.O/blob/24f8e6bce2f91462ac8ffed25b92be7354c2bd16/app_backend/server.py#L160-L177) и [do_GET #L205-L261](https://github.com/jumonja-maker/A.I.O/blob/24f8e6bce2f91462ac8ffed25b92be7354c2bd16/app_backend/server.py#L205-L261). GET `/` без предварительной проверки HTTP `Host` отдаёт HTML с `data-aio-token`. Guard API проверяет Host и Origin на действиях, но статическая точка выдачи секрета его не проверяет ([http_api.py #L56-L66](https://github.com/jumonja-maker/A.I.O/blob/24f8e6bce2f91462ac8ffed25b92be7354c2bd16/app_backend/converter_guard/http_api.py#L56-L66)). Сервер действительно привязан к `127.0.0.1`, поэтому это *потенциальный*, а не доказанный рабочий вектор DNS-rebinding/origin-confusion.

**Требуемая приёмка:** raw HTTP GET `/` с правильным/неправильным/двойным Host; ошибочный Host должен получать отказ без выдачи токена. Валидация до HTML и для прочих UI ресурсов, без поломки Qt localhost. Отдельный issue [#43](https://github.com/jumonja-maker/A.I.O/issues/43).

## F-02 [P1] Ложные негативы проверки потери ODF-ячеек

**Код:** [content_invariants.py #L113-L184](https://github.com/jumonja-maker/A.I.O/blob/24f8e6bce2f91462ac8ffed25b92be7354c2bd16/app_backend/converter_guard/content_invariants.py#L113-L184), [результат #L267-L315](https://github.com/jumonja-maker/A.I.O/blob/24f8e6bce2f91462ac8ffed25b92be7354c2bd16/app_backend/converter_guard/content_invariants.py#L267-L315). Источник хранит множество *уникальных* строк из проблемных таблиц; `_Presence.feed` ищет `token in window` без границ ячейки/полного значения и без multiplicity. Поэтому `42` в `1420`, `A` и `B` в `AB`, 4 одинаковые исходные ячейки `A` против 1 выходной `A` могут дать `missingUniqueCellTexts=0` и `checked_subset`. Исходник с явно объявленными колонками вовсе не попадает в этот специализированный invariant. **Это дефект покрытия проверки, а не доказательство реального повреждения v18.** `qualityCompared=false` сохраняется честно, но внутренний статус subset нельзя воспринимать как сохранение всех ячеек.

**Приёмка:** негативные синтетические ODT→DOCX/ODT/TXT: повторные значения, подстроки, потеря колонки, перенос значения в не связанную с таблицей область; не подтверждать сохранность, когда структурное сравнение отсутствует; сравнить multiplicity/табличный контекст там, где возможно, и явно маркировать `not_checked` иначе. Issue [#44](https://github.com/jumonja-maker/A.I.O/issues/44).

## F-03 [P1] `qualityCompared=true` без сравнения значений многостраничных XLSX

**Код:** [quality.py #L101-L155](https://github.com/jumonja-maker/A.I.O/blob/24f8e6bce2f91462ac8ffed25b92be7354c2bd16/app_backend/converter_guard/quality.py#L101-L155), [#L157-L166](https://github.com/jumonja-maker/A.I.O/blob/24f8e6bce2f91462ac8ffed25b92be7354c2bd16/app_backend/converter_guard/quality.py#L157-L166), [#L285-L315](https://github.com/jumonja-maker/A.I.O/blob/24f8e6bce2f91462ac8ffed25b92be7354c2bd16/app_backend/converter_guard/quality.py#L285-L315). `_xlsx_values` на более чем одном листе возвращает количество листов без `valuesDigest`. `compare_documents` считает сравнение выполненным при пересечении любых default count keys (например, `tables`, `images`, `formulas`, `charts`), даже если фактические значения ячеек не сравнивались. Два XLSX с 2 листами, одинаковыми структурными счётчиками и *разными значениями* могут закончить как `checked_subset`, `qualityCompared=true`, не выдав `CELL_VALUES_DIFFER`.

**Приёмка:** синтетические 2+ sheet XLSX с заменёнными числами/формулами; не утверждать сравнение значений, если digest отсутствует; отдельно маркировать `structureCompared` / `valuesCompared` и хотя бы `spreadsheet_values:not_checked`. Issue [#45](https://github.com/jumonja-maker/A.I.O/issues/45).

## F-04 [P2, задокументированный edge case] Конкурентный/прерванный save на POSIX FS без hardlinks

**Код:** [core.py #L40-L76](https://github.com/jumonja-maker/A.I.O/blob/24f8e6bce2f91462ac8ffed25b92be7354c2bd16/app_backend/converter_guard/core.py#L40-L76), [save_folder #L1949-L2117](https://github.com/jumonja-maker/A.I.O/blob/24f8e6bce2f91462ac8ffed25b92be7354c2bd16/app_backend/converter_guard/core.py#L1949-L2117). В специально предусмотренном fallback `O_EXCL` при отсутствии hardlinks запись происходит уже под финальным именем. При срыве/отмене может остаться **неполный файл с финальным именем**, в то время как `AIO-save-status.json` сообщает `failed/cancelled`. Программа это документирует и не подтверждает `saved`; исходники Guard остаются доступны. Не классифицируем как доказанную потерю A.I.O. Рекомендован тест физической exFAT/FUSE-папки, невозможности hardlinks, disk-full и отмены после первой порции, плюс явная маркировка неудачной папки и отсутствие ложного успешного уведомления.

## F-05 [P2] Идентичность бинарника и LibreOffice в результирующем отчёте

**Код:** [core.py _report #L1745-L1767](https://github.com/jumonja-maker/A.I.O/blob/24f8e6bce2f91462ac8ffed25b92be7354c2bd16/app_backend/converter_guard/core.py#L1745-L1767). Отчёт содержит маршруты, source/output SHA и фильтры, но не достоверный `app build SHA`, версию и SHA установленного LO. Предыдущий BLACK.BOX отчёт отдельно показывал `AIO_BUILD_IDENTITY_UNVERIFIED`. Для последующих пользовательских bug reports фиксировать версию/коммит/хеш поставки и LO без чтения содержимого документов и без глобального сканирования диска. Не критический runtime bug.

## Main release gate — проверки, не заменённые зелёным source CI

1. **Повторить NIKITA EXE/Setup из точного QA SHA**, а не пользоваться v14/v15 pinned сборочными workflows. Проверить установку/удаление, QtWebEngine, QtPdf/изображения, кнопки Save, крестик/фон окна состояния, Explorer shortcuts, брендинг, локальный сервер, неподписанные/повреждённые файлы. `AllInOne.spec` по-прежнему озаглавлен старым `no Converter Guard`; само по себе это не доказательство дефекта упаковки, но требует QA сборки.
2. **Исправленные положительные FULLTEST-образцы**: повторить 510 входов либо минимальную воспроизводимую матрицу; отдельные намеренно повреждённые input fixtures. Сопоставить старые P0 (EMZ/WMZ 17, ODF 96, HTML пустые) и многокадровые APNG/TIFF. Считать `not_checked` отдельно от успеха, сверять реальный результат и логи.
3. **Длинная очередь и I/O**: 1000+ файлов, 10–15 тысяч задач, pause/cancel/restart/recover, сохранение во время живой очереди, остановка приложения во время readback, disk-full, несохранённые файлы на повторном запуске. Проверить RSS/handles Qt, а не только tracemalloc. Дополнительная проверка Excel на том же Windows EXE.
4. **Только честное сравнение производительности**: одинаковые inputs, версии LO, нативные фильтры, шрифты, disk, cold/warm условия; микробенчмарк status 29.67→0.165ms (legacy vs delta **в рамках нового кода**) не равен ускорению 14 417 реальных конвертаций.
5. **Заведомо открытые ограничения**: ORP без нативного exporter остаётся недоступным; clipboard issue #27 отложен; ODC/EPUB и структурная/визуальная fidelity на полной матрице пока не подтверждены. Наличие 322 изменённых после Drive HTML из старого прогона требует отдельной поэтапной атрибуции; не обвинять в ней v18 без проверки.

## Рекомендуемый порядок действий

**До merge `main`:** проверить/исправить #43 (локальный HTTP Host), #44–#45 (точность статусов контроля качества), затем собрать QA установщик на точном commit и провести короткую Windows приёмку сохранения/GUI. После — существенная часть FULLTEST на корректных позитивных и негативных образцах. `main` оставлять неизменной до согласия владельца после просмотра доказательств. Оптимизацию v18 в QA сохранять, собственную сборку LibreOffice не создавать.

**Ограничение этого аудита:** не выдаём статические предположения за уже воспроизведённые баги в установленном EXE. Три P1 — конкретные пропуски/условия в коде, которые имеют ясные негативные тест-кейсы; настоящий live-run для них ещё не выполнен. 
