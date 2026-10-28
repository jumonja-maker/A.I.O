# Блокирующий отказ первого совместного stress3

Run https://github.com/jumonja-maker/A.I.O/actions/runs/40607773754 — FAIL; job 122551460036.
Source `6b61cb1413e753e687d1ff61749e1164069a3c1c`, tree `a9717652ec545c3a2b3f47c58f9b3b3aef8abf41`.

24 native OOXML/PDF controls и 12 parser/identity controls прошли. DIF 48/48, текстовые ID 108/108 прошли. Девять непосредственных all-sheets конвертаций прошли, но реальный retained queue → partial save/recovery не запустил экспорт: PREFLIGHT_ROUTE_UNDETERMINED после 44 секунд. Тест сохранения не ослаблен; весь remaining corpus, соседи и общий source suite не исполнялись. False rejection/silent success metrics остаются null.

Все 9 исходных JSON/CSV доказательств восстановлены из stdout bundle побайтно с проверкой SHA256, UTF8/BOM/CRLF. Они сохранены в evidence/remaining-bag/native/40607773754; capture-index.json содержит исходные SHA и Git blob IDs. Не удалено ни одно пользовательское испытание.

Проверяется гипотеза: длинный вложенный workspace создаёт недопустимо длинный внутренний native profile. На неизменном product source выполняется A/B native import того же retained исходника с обычным и отдельным коротким тестовым профилем. До результата диагноз не объявляется доказанным. Это диагностический прогон, а не приёмка исправленного продукта. Объединение кандидата в QA заблокировано.
