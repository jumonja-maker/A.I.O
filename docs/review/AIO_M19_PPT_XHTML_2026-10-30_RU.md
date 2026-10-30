# A.I.O — M19: ограниченное восстановление текста PPT → XHTML и гонка legacy gate

**Дата:** 30.10.2026. **QA-ветка:** `fix/fulltest-critical-hosted-lo-20261029`. **Продуктовый SHA:** `b90ea027e0178d010791f1137c3fb83b0acc1ba2`. **Подтверждённый CI:** [41055536641](https://github.com/jumonja-maker/A.I.O/actions/runs/41055536641) — completed/SUCCESS, Windows + Linux + общий AIO QA gate. MAIN, base PR и personal runners не изменены, PR #83 остаётся Draft.

## Причина: M19 был реальной потерей, а не ошибкой теста

Повторена native цепочка FODP → PPT → XHTML на доступном локальном LibreOffice 25.2.3.2: FODP→XHTML содержит все три маркера `AIO_SLIDE_1..3`; старый PPT→XHTML формирует структурно допустимый XHTML, но все три маркера исчезают. Прямой `XHTML Draw File` и промежуточные ODP/FODP/PPTX не восстановили текст. Нативный `impress_html_Export` в HTML сохраняет текст **лишь как плоский outline** — он не подменён успешным XHTML, чтобы не потерять графику незаметно.

UNO-инспекция показывает: FODP содержит `com.sun.star.drawing.TextShape`, а импортированный PPT — `com.sun.star.drawing.CustomShape` с `CustomShapeGeometry.Type=mso-spt202` и читаемым `getString()`. XHTML-фильтр LibreOffice этот текст пропускает. В отдельном локальном native UNO-опыте добавление временных `TextShape` на те же страницы и координаты дало XHTML со всеми тремя маркерами. Это установленный факт для **исследованных** образцов/движков, не доказательство универсальной совместимости.

## Продуктовое изменение (4 файла backend + native QA + 1 новый test)

1. В `native_fidelity.py` добавлена `aio_try_ppt_xhtml_textboxes`. Она работает **только** после обнаруженной `AioPresentationTextLoss` в текущем native XHTML-экспорте из `.ppt` и с фильтром `XHTML Impress File`. Старые PPT с изображениями, группами, нетекстовыми фигурами или незнакомой геометрией не допускаются; они сохраняют безопасный отказ. Число страниц ограничено 32, обследованных фигур — 256. Отдельный native HTML outline или сторонний конвертер не используется.
2. В существующем приватном UNO-моделе временно создаются нативные `TextShape` только для распознанных `mso-spt202` текстовых полей. Сохраняется отдельный кандидат XHTML; каждый ожидаемый фрагмент текста проверяется чтением **фактического** результата. Лишь затем результат атомарно заменяет исходный XHTML. При отказе оригинальный выход не подменяется, временный кандидат удаляется, исходный PPT **не сохраняется/не меняется**.
3. Guard получает явное предупреждение `NATIVE_PPT_XHTML_TEXTBOX_RECOVERY` и `NATIVE_OUTPUT_NOT_CHECKED`, а также resultCheck `native_ppt_xhtml_textbox_recovery`, `checked_subset`, `textPresent=true`, **`qualityCompared=false`**. Не объявляются проверенными визуальная эквивалентность, layout, графика master-slide, анимации и ссылки. Это узкое восстановление текста, а не новая универсальная технология PPT-конвертации.
4. Для прежнего A06 дополнительно исправлен `converter_gate`: если `begin()` отклонил запрос до создания `Request`, семафор освобождается **до** HTTP ответа 429/422. Не меняется освобождение успешно начатых заданий после завершения. Предыдущий документационный CI [41051093896](https://github.com/jumonja-maker/A.I.O/actions/runs/41051093896) выявил Linux-гоночное падение `test_capacity_rejection_does_not_read_body_and_releases_engine_gate` на старой реализации (HTTP ответ приходил раньше finally). Проверка оставлена строгой; исправлено поведение сервера, не тест.

## Проверка и доказательства

| Проверка | Подтверждено |
|---|---|
| Windows source suite | **1339 discovered / 1332 PASS / 7 platform N/A / 0 FAIL/ERROR** |
| Linux source suite | **1339 discovered / 1335 PASS / 4 platform N/A / 0 FAIL/ERROR** |
| Новые регрессии | **6 PASS** на каждой платформе; входят в 1339 |
| Native Windows LO 26.8.2.2 | **16/16 PASS** в batch1; `.ppt_xhtml` **PASS, textRetained=true**, ранее **SAFE_REJECTION/ENGINE_OUTPUT_CONTENT_LOSS** |
| Исходный тестовый PPT | SHA-256 до/после XHTML совпадает |
| Старая A06 recovery/capacity | Все прежние тесты PASS, включая немедленное приобретение converter_gate после HTTP 429 |
| Windows EXE и bundled LO | **PASS**, LO 26.8.2.2, embedded source SHA и dependency manifest привязаны к `b90ea02` |
| Frozen structural worker | PASS, но desktopGUI/LibreOffice именно внутри этого smoke **NOT_RUN** |
| Итоговые platform gates и AIO QA gate | **PASS на всех трёх jobs** |

Windows job `124062001776`; Linux `124062002366`; общий gate `124065053769`. У Windows проверен LO 26.8.2.2 / `9dea406ab7695df6325b11b0e19fdca8db4b7276`, официальный MSI SHA-256 `ff4b911403b67ee9e6249f820f9782c44bb32c94fdaa45b0da53a8e419e5307a`. Linux использует системный движок, а M19 native контроль заявлен для закреплённого Windows.

## Статус и остаток

**M19 — ЧАСТИЧНО.** Теперь есть доказанный рабочий путь для ограниченного вида старого PPT, но сложные презентации, рисунки, нестандартная геометрия и визуальная верность не закрыты. Результат помечен честными предупреждениями. M20/M21, M14 и исторический FULLTEST **200 файлов / 5723 задач** остаются открытыми; **1079 исторических отклонений не пересчитаны**.

Сопоставимый benchmark, установленный GUI/DPI, реальное сохранение на пользовательский диск, установщик и релиз не запускались. Это не merge MAIN. Следующий приоритет — проверка M14 на реальном Windows native registry с тестовыми источниками и повторное изучение M20/M21 по исходным данным.