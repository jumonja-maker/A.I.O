# LibreOffice: проверяемые источники качества и скорости

Дата сбора: 2026-11-08T04:44:55.770303+00:00

16 основных источников: 7 официальных Help-разделов, 7 Bugzilla сообщений (одно UNCONFIRMED), 2 пользовательских обсуждения. Дополнительно проверены уже исправленные SmartArt/integer bugs, NOTABUG обсуждение CSV numeric fields, отдельные описания VBA и Precision as shown. Это источники рисков, а не доказательство всех дефектов в точной текущей сборке.

## Как читать выводы

- Официальная граница формата или настройки отличается от воспроизводимого дефекта движка.
- У open bugs ниже указан последний осмысленный retest; массовый QA-ping не считается проверкой новой версии.
- Отзыв пользователя подтверждает факт сообщения, но не его причину и не актуальность на LO 26.8.2.
- Для текущего AIO отдельно нужны fixture, exact engine/filter/options, independent oracle, исходный и выходной SHA, time/RSS.
- У code660b470 PDF lossless механизм существовал как optional retry; обычный экспорт не был тем самым retry.

## Реестр

### LO-S01 — About Converting Microsoft Office Documents

https://help.libreoffice.org/latest/en-US/text/shared/guide/ms_import_export_limitations.html

**Наблюдение:** Сложная верстка Office и некоторые свойства обрабатываются иначе либо не поддерживаются. Официально перечислены Word AutoShapes/ревизии/OLE/поля/индексы/таблицы/frames/колонки/ссылки/WordArt, PowerPoint master background/grouping/multimedia и Excel pivot/new charts/conditional formatting/functions. Есть конкретный пример разных результатов SUM для логических ячеек Calc и Excel.

**Граница вывода:** Это общая официальная граница совместимости, а не доказательство потери в каждом DOCX/XLSX/PPTX. Документирование функции не означает её полного отсутствия или обязательной порчи.

**Ответственность AIO / проверка:** Нельзя удостоверять полную сохранность только по открытию файла или успешному native export. Сохранять исходник, фиксировать класс непроверенных свойств, сравнивать маркеры, числа и визуальные эталоны отдельно.

Короткая цитата: “some layout features and formatting attributes ... are handled differently ... or are unsupported”.

Сохранённые свидетельства: official_ms_limitations.raw.

### LO-S02 — PDF Export General

https://help.libreoffice.org/latest/en-US/text/shared/01/ref_pdf_export_general.html

**Наблюдение:** JPEG compression и понижение DPI меняют пиксели. EPS с embedded preview попадает в PDF только как preview, а EPS без preview — как пустой placeholder. Настройки print ranges, comments, hidden slides, notes и blank pages меняют экспортируемый состав и иногда масштаб страницы.

**Граница вывода:** Ограничение EPS прямо документировано. Потеря интерактивной семантики при PDF сама по себе не является багом; выбранная print/export policy должна быть определена заранее.

**Ответственность AIO / проверка:** Явно задавать применимые параметры; не выдавать nonempty PDF/одинаковое число страниц за наличие исходного EPS. Описывать ожидаемые потери и не включать hidden/private content автоматически ради формального числа страниц.

Короткая цитата: “EPS images without embedded previews are exported as empty placeholders.”.

Сохранённые свидетельства: official_pdf_export.raw.

### LO-S03 — PDF Export Command Line Parameters

https://help.libreoffice.org/latest/en-US/text/shared/guide/pdf_params.html

**Наблюдение:** По документации UseLosslessCompression по умолчанию false, JPEG Quality=90, ReduceImageResolution=false; MaxImageResolution=300 применяется при включённом resampling. ExportNotes/ExportNotesPages/ExportHiddenSlides по умолчанию false. SinglePageSheets игнорирует print ranges и hidden status; EmbedStandardFonts касается именно 14 стандартных PDF fonts, а не всех шрифтов документа.

**Граница вывода:** Это значения документированных defaults, не измерение текущего пользовательского профиля. Параметры может переопределить AIO или конфигурация LO. Lossless не восстанавливает то, что потеряно уже при импорте.

**Ответственность AIO / проверка:** В начальном коде 660b470 lossless/no-resample были реализованы в optional automatic_recipe (pdf_preserve_images), но не в ordinary native_export_recipe. Первичный Guard и legacy должны проверяться отдельно. Нельзя менять состав notes/slides произвольно. После исправления отмечать baseline отдельно от final.

Короткая цитата: “Specifies if images are exported to PDF using a lossless compression format like PNG or compressed using the JPEG format.”.

Сохранённые свидетельства: official_pdf_params.raw.

### LO-S04 — Fonts

https://help.libreoffice.org/latest/en-US/text/shared/optionen/01010700.html

**Наблюдение:** Отсутствующий шрифт может заменяться ОС/LO; замена влияет на экран и печать, но не меняет сохранённое имя шрифта в документе. Поэтому метрика font family в XML не доказывает одинаковый рендеринг.

**Граница вывода:** Это ограничение окружения/настройки, не автоматически ошибка парсера LO. Даже открытие на другой машине с тем же XML может дать другой перенос строк и пагинацию.

**Ответственность AIO / проверка:** Фиксировать набор реально установленных шрифтов и фактическую замену; для эталона использовать ту же версию шрифта и движка. Не обещать исправление просто сохранением исходного font name.

Короткая цитата: “The replacement does not change the font settings that are saved in the document.”.

Сохранённые свидетельства: official_font_substitution.raw.

### LO-S05 — Opening and Saving Text CSV Files

https://help.libreoffice.org/latest/en-US/text/scalc/guide/csv_files.html

**Наблюдение:** Обычный CSV сохраняет только данные одного листа; оформление и редактируемые формулы не сохраняются. Отдельная официальная страница csv_formula показывает явный выбор формул-текста либо рассчитанных значений.

**Граница вывода:** CSV — однотабличный текстовый формат; это ожидаемая потеря свойств формата. При этом CLI/UNO имеет специальные способы экспортировать несколько листов в несколько файлов, поэтому нельзя говорить «LO никогда не умеет все листы».

**Ответственность AIO / проверка:** Проверять все листы/выходные имена и manifests; явно обозначать values-versus-formulas policy и потерю оформления. Native first-sheet success без такой политики не равен полному преобразованию книги.

Короткая цитата: “only the data on the current sheet is saved. All other information, including formulas and formatting, is lost.”.

Сохранённые свидетельства: official_csv.raw, official_csv_formula.raw.

### LO-S06 — CSV Filter Parameters

https://help.libreoffice.org/latest/en-US/text/shared/guide/csv_params.html

**Наблюдение:** CSV-параметры управляют charset, delimiters, quoted-as-text, special-number detection, display-versus-raw values, formula evaluation и export sheets. Save cell contents as shown по умолчанию true может фиксировать уже округлённый вид; number/date/formula inference может изменить исходный текст.

**Граница вывода:** Само наличие токенов не доказывает, что AIO передало их правильному import/export filter. Выбор безопасного представления зависит от типа исходника и целевого формата.

**Ответственность AIO / проверка:** Явно задавать import/export policies раздельно, защищать leading zeros/long IDs/formula-like literals, проверять типы и точные значения независимым парсером. Не объявлять формулой текст только из-за знака =.

Короткая цитата: “Save cell contents as shown ... Default value: true.”.

Сохранённые свидетельства: official_csv_params.raw.

### LO-S07 — File Conversion Filter Names

https://help.libreoffice.org/latest/en-US/text/shared/guide/convertfilters.html

**Наблюдение:** Фильтры и расширения привязаны к моделям документов Writer/Calc/Draw/Impress и параметрам. Наличие совпадающего расширения не означает подходящий exporter для произвольной модели.

**Граница вывода:** Таблица возможностей не является обещанием полной сохранности каждой импортированной особенности. Не все расширения имеют экспортный фильтр, и некоторые имена охватывают разные диалекты.

**Ответственность AIO / проверка:** Продолжать использовать runtime registry/native proof, отличать импорт от экспорта и actual model от suffix. Unsupported route сообщать явно, а не переименовывать другой результат в нужный suffix.

Сохранённые свидетельства: official_filters.raw.

### LO-S08 — FILEOPEN DOCX : The use of vertical and horizontal typesetting results in the loss of text content and typesetting style

https://bugs.documentfoundation.org/show_bug.cgi?id=134670

**Наблюдение:** DOCX со смешанным вертикальным/горизонтальным CJK-набором терял текст и оформление при импорте; есть минимальный публичный DOCX reproducer.

**Граница вывода:** Статус NEW подтверждает открытое сообщение, но не воспроизведение в точном Windows LO 26.8.2 или Linux LO 26.8alpha этой среды. Последнее обновление 2026-08-27 — автоматический QA MassPing, а не retest 26.8.

**Ответственность AIO / проверка:** Подобные тексты требуют отдельного sentinel/visual oracle; подсчёт paragraph/file existence недостаточен. Сначала повторить fixture на точном engine; не переносить старый LO bug в список исправленных AIO bugs.

Статус: NEW; поле version: 4.1 all versions; создан: 2020-07-09T03:10:46Z; последнее изменение: 2026-08-27T03:15:22Z.
Осмысленный retest: {"date": "2023-06-08", "version": "7.6+", "note": "comment: repro 7.6+"}.

Сохранённые свидетельства: bug_134670.json, bug_134670_comments.json.

### LO-S09 — Calc ignores cell line breaks when opening .xlsx files containing multiple sheets — occasional loss of newline in cells (≈1 in 30 opens)

https://bugs.documentfoundation.org/show_bug.cgi?id=169772

**Наблюдение:** В многостраничных XLSX иногда пропадали/игнорировались внутренние переводы строк ячейки; reporter оценивал частоту около 1/30 открытий. Подтверждено на Windows 26.2.2.2, регрессия связана с импортом multiline text начиная с 7.6beta.

**Граница вывода:** Статус NEW подтверждает открытое сообщение, но не воспроизведение в точном Windows LO 26.8.2 или Linux LO 26.8alpha этой среды. 

**Ответственность AIO / проверка:** Не нормализовать все пробелы до проверки значимых linebreaks/строк; для вероятностного дефекта один успешный импорт не является достаточным отрицательным тестом. Повторять на копиях и фиксировать wrap-text/rendering отдельно от фактических символов ячейки.

Статус: NEW; поле version: 7.6.0.0 beta1+; создан: 2025-12-01T03:07:47Z; последнее изменение: 2026-10-14T11:04:35Z.
Осмысленный retest: {"date": "2026-04-18", "version": "26.2.2.2 Windows; 7.6.7.2 Windows", "note": "comment reports reproduced; 7.5.9.2 did not reproduce after 40 opens"}.

Сохранённые свидетельства: bug_169772.json, bug_169772_comments.json.

### LO-S10 — FILESAVE Export to formats that don't include margins (PNG and GIF with transparency; EMF; WMF; EPS) is distorted / stretched

https://bugs.documentfoundation.org/show_bug.cgi?id=122155

**Наблюдение:** Draw-экспорт в некоторые прозрачные raster/metafile форматы растягивал рисунок при различии полей страницы и области содержимого. В комментарии отдельно подтверждены PNG/GIF при transparency, EMF/WMF/EPS.

**Граница вывода:** Статус NEW подтверждает открытое сообщение, но не воспроизведение в точном Windows LO 26.8.2 или Linux LO 26.8alpha этой среды. 

**Ответственность AIO / проверка:** Сравнивать aspect ratio/геометрию объектов, а не только размер canvas и число страниц. Не отключать transparency молча ради прохождения теста; это меняет исходную визуальную семантику.

Статус: NEW; поле version: 3.4.0 release; создан: 2018-12-17T13:42:09Z; последнее изменение: 2026-03-15T01:08:54Z.
Осмысленный retest: {"date": "2024-06-18", "version": "24.8.0.0.alpha1+ Linux", "note": "PNG/GIF issue not reproduced when transparency off; EMF has no such option"}.

Сохранённые свидетельства: bug_122155.json, bug_122155_comments.json.

### LO-S11 — Exporting .ods graph to .svg image loses x-axis graph values

https://bugs.documentfoundation.org/show_bug.cgi?id=162775

**Наблюдение:** При экспорте ODS-графика в SVG пропадали подписи значений x-axis; PNG/JPEG в этом сообщении работали ожидаемо. Непустой SVG не доказывает сохранность графика.

**Граница вывода:** Статус NEW подтверждает открытое сообщение, но не воспроизведение в точном Windows LO 26.8.2 или Linux LO 26.8alpha этой среды. Обновление 2026-09-23 — QA MassPing, не свежая успешная/неуспешная проверка.

**Ответственность AIO / проверка:** Проверять labels/legend и видимость текста в SVG; наличие текста в source model или пустых defs не является доказательством видимой подписи. Если нужен workaround raster, явно указать rasterization.

Статус: NEW; поле version: 24.8.0.3 release; создан: 2024-09-03T17:29:36Z; последнее изменение: 2026-09-23T04:18:05Z.
Осмысленный retest: {"date": "2024-09-03", "version": "25.2.0.0.alpha0+ Windows"}.

Сохранённые свидетельства: bug_162775.json, bug_162775_comments.json.

### LO-S12 — PDF: slow export with "reduce image resolution" or "JPEG compression"

https://bugs.documentfoundation.org/show_bug.cgi?id=163607

**Наблюдение:** PDF export с JPEG compression и resampling был существенно медленнее lossless/no-resample. В сообщении 26.2alpha приведены около 6s с JPEG90/300DPI против около 2s без одного из этих преобразований. В 2026-07 подтверждено воспроизведение на developer 27.2alpha.

**Граница вывода:** Статус NEW подтверждает открытое сообщение, но не воспроизведение в точном Windows LO 26.8.2 или Linux LO 26.8alpha этой среды. 

**Ответственность AIO / проверка:** Измерять import/export/validation/save раздельно и прогонять несколько повторов. Lossless может одновременно улучшить качество и сократить перекодирование, но увеличивает байты; универсального обещания ускорения нет. Не выключать PDF/A/tagging без явной политики.

Статус: NEW; поле version: 24.2.6.2 release; создан: 2024-10-24T16:06:42Z; последнее изменение: 2026-07-07T18:56:30Z.
Осмысленный retest: {"date": "2026-07-07", "version": "27.2.0.0.alpha0+ Linux", "note": "Still repro; earlier 26.2alpha A/B and public ODP attachment203331"}.

Сохранённые свидетельства: bug_163607.json, bug_163607_comments.json.

### LO-S13 — SVG in Writer leads to huge and slow-rendering PDF export

https://bugs.documentfoundation.org/show_bug.cgi?id=140963

**Наблюдение:** Вставленный SVG около 400KiB приводил к PDF около 13MB с медленным рендерингом в нескольких viewers. Есть публичные ODT/SVG/PDF и повторные подтверждения на 24.8,25.2 и 26.2alpha.

**Граница вывода:** Статус NEW подтверждает открытое сообщение, но не воспроизведение в точном Windows LO 26.8.2 или Linux LO 26.8alpha этой среды. 

**Ответственность AIO / проверка:** Проверять не только время конвертации, но output growth и последующий first render/scroll. Нельзя молча rasterize vector ради уменьшения файла; это отдельная quality tradeoff.

Статус: NEW; поле version: 7.1.0.3 release; создан: 2021-03-11T21:33:59Z; последнее изменение: 2025-09-10T17:03:49Z.
Осмысленный retest: {"date": "2025-09-10", "version": "26.2.0.0.alpha0+ Linux"}.

Сохранённые свидетельства: bug_140963.json, bug_140963_comments.json.

### LO-S14 — Image Quality problem during Word to Pdf Document Conversion

https://ask.libreoffice.org/t/image-quality-problem-during-word-to-pdf-document-conversion/88258

**Наблюдение:** Пользователь LO 7.4 сообщил о низком качестве изображений после обычного headless DOC/DOCX→PDF и привёл CLI-команду без FilterData.

**Граница вывода:** Сообщение 2023-02-20 не содержит независимой причины, эталона или принятого решения. Это отзыв/симптом, не доказательство актуального бага 26.8.2. Возможная роль defaults объясняется официальными S02–S03 и должна подтверждаться A/B на своём файле.

**Ответственность AIO / проверка:** Не обобщать жалобу до «весь LO портит картинки». Сравнить primary/default export с explicit lossless/no-resample и исходными pixels; фиксировать параметры конкретного запуска.

Короткая цитата: “the image Quality after conversion is very low”.

Сохранённые свидетельства: ask_image_quality.raw.

### LO-S15 — How to convert a huge xls to pdf using LO CLI

https://ask.libreoffice.org/t/how-to-convert-a-huge-xls-to-pdf-using-lo-cli/36024

**Наблюдение:** Исторический пользовательский пример разделения широких колонок по страницам XLS→PDF. Ответ указывал на print scaling; автор затем сообщил, что новая версия LO решила первоначальный случай, но задавал отдельный вопрос о качестве изображений.

**Граница вывода:** Первый пример относится к 5.4.2.2 в 2018 году. Внутри самого обсуждения есть сообщение об улучшении после обновления, поэтому нельзя включать его как текущий незакрытый bug. Его «CLI не умеет» нельзя переносить на современные SinglePageSheets/UNO настройки без проверки.

**Ответственность AIO / проверка:** Определить print ranges/scaling и нужную пагинацию; не путать ожидаемое разбиение печатного листа с потерей ячеек. Не включать SinglePageSheets молча: этот режим включает даже hidden sheets.

Сохранённые свидетельства: ask_huge_xls.raw.

### LO-S16 — FILESAVE XLSX: numbers saved with only 15 significant digits, so values change on round-trip

https://bugs.documentfoundation.org/show_bug.cgi?id=174261

**Наблюдение:** Свежий reporter claim для 26.8.2: XLSX сохраняет 0.333333333333333 вместо 0.3333333333333333 и 0.3 вместо 0.30000000000000004; заявлены изменённые doubles при массовом round-trip. Это отличается от первоначального импорта неизвестной схемы CSV.

**Граница вывода:** Статус именно UNCONFIRMED, а не NEW/FIXED; численные 500000/447375 — утверждение автора сообщения, не измерение AIO-аудита. Поле version26.8.2.2 не заменяет локальный repro на pinned engine.

**Ответственность AIO / проверка:** Отдельно сравнивать raw XML numeric tokens и фактические doubles до и после native XLSX/FODS round-trip. Не смешивать с уже исправленным integer-case160985 или schema policy CSV. Окончательный статус локальных измерений устанавливает engine-аудит.

Статус: UNCONFIRMED; поле version: 26.8.2.2 release; создан: 2026-10-31T04:20:35Z; последнее изменение: 2026-10-31T04:26:05Z.
Осмысленный retest: {"status": "not independently confirmed; original reporter only"}.

Сохранённые свидетельства: bug_174261.json, bug_174261_comments.json.

## Проверенный старый исправленный случай

https://bugs.documentfoundation.org/show_bug.cgi?id=149840 — SmartArt background size, RESOLVED/FIXED, fix доступен в 7.5.0 по комментарию с commit. Не включать как текущий дефект 26.8.2.

https://bugs.documentfoundation.org/show_bug.cgi?id=160985 — integer XLSX export, RESOLVED/FIXED, backport24.2.4 и master24.8.0. Отдельный свежий174261 имеет статусUNCONFIRMED; их нельзя объединять.

https://bugs.documentfoundation.org/show_bug.cgi?id=150635 — CSV/text policy, RESOLVED/NOTABUG. Отсутствие schema и ограничения binary numeric model не равны новому багу AIO.

## Что полезно измерить в приложении

1. Один и тот же файл через прямой LO, Guard, legacy и итоговое сохранение; отдельно импорт, экспорт, validation и copy/readback.
2. PDF image pixels до/после default JPEG и explicit lossless, размеры embedded images, наличие EPS previews, notes и выбранных slides.
3. Верстку и шрифты на fixed font inventory; исходное сохранённое имя шрифта не равно реально использованному font face.
4. CSV не только по строкам: leading zeros, long IDs, type, decimal precision, literal =, dates и все sheet outputs.
5. PDF/vector output growth и скорость первого рендера/скролла, а не только return code конвертера.
6. Известные engine bug fixtures на точной pinned Windows версии, без объявления старого NEW сообщения автоматически текущим.

## Текущие локальные измерения двух официальных reproducer-файлов

Выполнил performance-аудит, не research-agent. Движок: LibreOfficeDev 26.8.0.0.alpha0 2c87e51eeaa2b413ff4ae097b2705eea1995d8e5.
Scope: one ordered sample per option, cold private LO profile; absolute times include startup; not a statistical benchmark, visual certification or AIO overhead measurement.

| Bug / fixture | Настройка | Время экспорта | Размер PDF | Страниц |
|---|---|---|---|---|
| #163607 | jpeg90_300dpi | 4.571 s | 593308 B | 32 |
| #163607 | lossless_no_resample | 1.406 s | 668296 B | 32 |
| #140963 | jpeg90_300dpi | 3.621 s | 13707962 B | 1 |
| #140963 | lossless_no_resample | 3.906 s | 13707962 B | 1 |

У ODP lossless/no-resample в этом единичном прогоне быстрее, но PDF больше. У SVG/ODT смена image options не устранила рост до13.7MB. Тождество first-page RGB двух вариантов SVG не является сравнением с независимым original-render oracle. Не объявлять весь LibreOffice ускоренным, visual fidelity доказанной или upstream bug исправленным.

Полное свидетельство: /workspace/scratch/aio-deep-20261108/performance/official-lo-native-benchmark.json. Файлы внешних reproducer-документов оставлены в scratch; metadata URL/date/SHA сохранена отдельно.
