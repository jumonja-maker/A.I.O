# Досмотр OOXML numeric contract

Просмотрены `spreadsheet_numeric_contract.py`, подключение в Guard/postconditions/SourceEvidence и legacy transport. Исходники и тесты reviewer не менял; LibreOffice не запускал.

## Найдено и устранено автором изменения

В первой версии helper отдельный `<f>` исключал только anchor ячейку. У array/shared/dataTable formula область `ref` может содержать follower-ячейки с кешированными `<v>` без собственного `<f>`. Их числовой кеш ошибочно считался исходным литералом, что давало ложный `ENGINE_OUTPUT_CONTENT_LOSS`.

Независимое воспроизведение: `numeric_formula_probe.py`, синтетические XLSX в `numeric-formula-fixtures/`. До исправления: `numeric-formula-probe-before.json`, helper SHA-256 `c0ee283b92231690b713b0cbbe533ce74de48a69c2f2043fe98d0875a5b4f94d`, ошибочный content loss. После исправления: `numeric-formula-probe.json`, helper SHA-256 `708c1f16c0f1e057bb56a3b15299ed81e67305da9ab3298b8c4372d594b61c2e`, возврат `None`, поскольку оба числовых значения относятся к formula cache.

Автор добавил ограниченные прямоугольники formula-ref без разворачивания сетки: источник исключает follower-кеши, а output formula-range не может подменять исходный константный литерал. Неподдержанная ref-грамматика и превышенный бюджет остаются явно `not_checked`.

## Финальная проверка

- 16 новых целевых тестов: PASS, `numeric-focused-tests.log`.
- 18 дополнительных независимых control-flow проб: PASS. Проверены ошибки `LibreOfficeError/LEGACY_STORAGE_BYTES`, `GuardError/SPOOL_QUOTA` и отмена на ранних и поздних опросах, с исходным SourceEvidence-кешем и без него. Сохраняются исходные объекты исключений, а не `not_checked` XML-ошибки. Скрипт `numeric_control_probe.py`, результат `numeric-control-probe.json`.
- Проверены raw numeric XML, OPC-связи нестандартных worksheet parts, sheet name + coordinate mapping, тип результата, отсутствие подмены констант formula/cache-значениями, бюджет и отдельное перечитывание каждого output.
- `git diff --check`: чисто.

**После исправления блокирующих замечаний по рассмотренному helper/hooks нет.**

Граница гарантии: сравнивается заявленное подмножество исходных OOXML numeric literals с более чем 15 значащими десятичными цифрами; интегральные значения — exact Decimal, неинтегральные — один IEEE-double value. Это не проверка всех чисел, формул, кэшей, форматов или полной эквивалентности книги. Неподдержанные источники/targets и исчерпанная инспекция не становятся подтверждённым числовым совпадением. Полный suite и Windows/native должен выполнить root.
