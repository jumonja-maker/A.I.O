# Дополнение P1: сырые числовые литералы OOXML

Root воспроизвёл реальным LibreOffice: исходный XLSX B2=10000000000000002 и B3=12345678901234567 после преобразования изменяются на10000000000000000 и12345678901234600. Исходные XML-литералы точны; это другой путь, чем CSV, поэтому CSV Text policy его не защищала. Native evidence root: root/native-deep/evidence.json.

## Исправление

Новый shared helper `spreadsheet_numeric_contract.py` используется Guard postconditions и Legacy execute до публикации ticket/journal/artifacts. Источник распознаётся по содержимому OOXML spreadsheet, не по суффиксу. Workbook/worksheet parts разрешаются через OPC relationships, включая нестандартные пути. Все реальные ячейки обходятся потоково с идентичностью листа и координатой; worksheet dimension hints не ограничивают чтение.

Отслеживаются только исходные numeric literals с более чем15 значащими цифрами. Интегральные литералы сравниваются как точные Decimal, включая эквивалентную exponent-запись. Неинтегральные сравниваются в IEEE-double domain:0.10000000000000001→0.1 не считается потерей, если double одинаков. Строка/формула вместо исходной numeric cell считается изменением типа. Пропавшая отслеживаемая ячейка/лист, изменённая числовая семантика или тип дают ENGINE_OUTPUT_CONTENT_LOSS / LEGACY_OUTPUT_CONTENT_LOSS до ready. Исходники и не опубликованные native outputs остаются для восстановления.

Source formula cells, cm/vm metadata cells и все последователи объявленных formula ref ranges(array/shared/dataTable) исключены из literal-обещания. Объявленные прямоугольники не расширяются в миллион ячеек; ranges и candidates ограничены бюджетами. Output literal, который стал частью формульного range, отвергается.

Source subset кэшируется через SourceEvidence; каждый output независимо перечитывается. Неизвестный контейнер, неподдержанная грамматика/диапазон, исчерпание inspection budget дают not_checked и явное предупреждение, не доказательство повреждения и не сертификат сохранности. Storage-control exceptions и cancel не маскируются под XML/number ошибки.

## Проверки

Два integration regression до подключения hooks падали: Guard принимал изменённые числа, Legacy публиковал ticket. После:16 новых focused методов PASS, включая exactints, float lexical-equivalence, type/formula change, sheet mapping, missing cells, nonstandard OPCsheet part, renamed source, array/shared/dataTable caches, huge formula range без expansion, cache/re-read, cancellation, budgets и Legacy publication. Raw values в diagnostics не включаются.

Независимый reviewer выполнил ещё18 late-control probes PASS, проверил raw array follower-cache repro и не нашёл блокирующих замечаний. Его отчёт: ../review/NUMERIC_REVIEW.md. Чужой test_jpeg_metadata_source_probe_still_allows_native_recovery выявил ошибку media-патча при расширенном targeted запуске; root/media уведомлены, этот numeric helper не менял media. В логах ранних compatibility harness также сохранены ошибочные имена testmodules — не ошибки AIO.

## Точные границы

Это high-precision literal subset, не проверка всей книги, формул, вычислений, форматирования, dates/styles, external links, tracked changes или complete workbook fidelity. Source calcPr/fullPrecision/Precision-as-shown авторская политика не интерпретируется: фактическое изменение хранимого raw literal может вызвать защитный отказ, даже если авторским отображением допускалось округление. Такой отказ не доказывает новый баг LibreOffice.

Поддерживается OOXML spreadsheet source с recognized Calc contract→XLSX/XLSM/XLTX/XLTM. Другие исходные контейнеры/targets не получают новых правил admission. Inspector budgets:64MiB XML суммарно на source/output проверку,4MiB metadata/member,10000 package members,1024 sheets,20000 tracked numeric cells,1000000 worksheet XMLnodes,512char numeric token,1024formula ranges и8сек deadline. Budgets не являются новым глобальным ограничением conversion-format; неизвестность видна в resultChecks/warnings.

Нативный повтор после numeric hook выполняет root; этот агент heavy native повтор не запускал. Source/test changes не коммитились дочерним агентом.
