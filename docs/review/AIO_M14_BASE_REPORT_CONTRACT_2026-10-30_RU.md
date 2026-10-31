# M14 — определение реального пакета отчёта LibreOffice Base (ORP)

Дата: 30.10.2026. База: интегрированная QA PR #83 на `68ed7754246bad8729a5ff0888961b2859897166`; MAIN не затронут.

## Воспроизведение

В приватном пользовательском FULLTEST нашлись исходные ORP с MIME `application/vnd.sun.xml.report`, корректным ZIP и XML с ODF `office:document-content/office:body/office:report`. Старый bounded SourceContract для всех MIME `application/vnd.sun.xml.*` ожидал древний namespace StarOffice 2000; `report` не было среди поддерживаемых legacy-классов, поэтому для настоящих ORP возвращалось `state=not_applicable`.

## Изменение

В существующем дешёвом `_odf_contract` добавлен узкий случай MIME `application/vnd.sun.xml.report` + правильные ODF root/body. Он возвращает только распознанное объявление `contentClass=base_report_package` с `expectedServices=['com.sun.star.report.ReportDefinition']`; неверные имена тела и корня отвергаются как подтверждённая структурная несогласованность. Не меняются импортёры, экспортёры, активный список расширений, процессная модель, workflow, UI, нативный registry, число сканирований и бюджет.

**Это НЕ открывает маршрут ORP.** Native preflight всё равно обязан открыть модель и подтвердить `ReportDefinition`, а FilterFactory/Guard должны согласовать конкретный экспорт. Если standalone LibreOffice/Base не может открыть файл отчёта без связи с базой, переход остаётся недоступным.

## Регрессии и доказательства

Новый `tests/test_base_report_contract_m14.py`: валидный ORP независимо от суффикса; контракт не выбирает фильтр и не допускает маршрут; ошибка MIME/body и root; неизвестный будущий legacy MIME; отсутствие content.xml. Тестовые файлы синтетические с теми же идентификаторами MIME/namespace; пользовательские оригиналы не копируются в публичный репозиторий.

Местное независимое исследование исходников на Linux LibreOffice 25.2.3.2: двум оригинальным ORP standalone CLI не смог загрузить документ и не создал PDF (даже при exit=0), поэтому не считать ORP исправленным или пригодным для массовой конвертации. Все исходники остались неизменными. Windows M14 synthetic CI #41080448645 уже завершён SUCCESS отдельно; данные исходников не заменяют нативный Windows-положительный Base ReportDefinition.

До прохождения нового Windows/Linux CI данный патч не QA-ПРОВЕРЕНО. M14 остаётся ОТКРЫТО; G02/FULLTEST и внешний Base positive не выполнены. Никакого merge MAIN и релиза.