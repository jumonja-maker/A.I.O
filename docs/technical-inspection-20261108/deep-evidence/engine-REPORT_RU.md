# Глубокий аудит границы движка A.I.O — результаты

Основа новой фазы: `660b47002672a3ad86e4ce8b1cd7c514df178295` (предыдущий PR103).

## Подтверждено и исправлено в рабочем дереве

1. **Один текстовый span подтверждал два разных параграфа презентации.** Valid ODP содержит отдельные `Total` и `Grand Total`, XHTML содержит только `Grand Total`. До исправления host `check_native_output` возвращал presentation `checked_subset` с sourceTextParagraphs=2; embedded UNO verifier также принимал текст. Это валидаторное воспроизведение с потерявшим текст выходом, не утверждение, что текущий LO сам всегда теряет такой параграф.

   Исправление: существующий bounded exact allocator TI08 извлечён в `converter_guard/text_occurrences.py` как trusted stdlib source. Тот же код используется ODF/TXT, host presentation и embedded native verifier. Сохранены исторические правила нормализации native; native использует literal spans без word-boundary, host использует word-boundary. Исчерпание бюджета остаётся incomplete, жадная неудача не считается доказательством потери.

2. **Прямой legacy XLSX→CSV терял все листы кроме первого и мог публиковаться как ready.** Реальная контрольная книга имеет два листа с FIRST/SECOND-маркерами. CLI CSV содержит только FIRST. Независимый native FODS содержит оба листа и маркера.

   Исправление: `legacy_preservation.py` независимо считает листы content-confirmed OOXML/Strict/ODS/FODS и сравнивает с top-level primary output файлами перед публикацией. Отсутствующий лист даёт LEGACY_OUTPUT_CONTENT_LOSS; незавершённая проверка известного Calc — LEGACY_OUTPUT_VALIDATION_INCOMPLETE. Неизвестный/бинарный source получает явный not_checked receipt. Receipt передаётся в targetReport.resultChecks и подтверждает только количество, не значения/оформление.

## Реальные native-наблюдения для документа

- HTML с тремя CSS page-break страницами →PDF: фактически 3 страницы с тремя уникальными маркерами. Linux actual LOK proof содержит Writer/Web/Global; TI09 корректно отказывает Guard PNG без all-page receipt. Этот путь не был ошибочно объявлен потерей после исправления.
- До новой PDF-политики default PNG64×64 RGB→PDF получил embedded JPEG: изменены 12212 из12288 каналов, maxerror234. Та же установленная LO и explicit lossless options сохранили все pixel bytes. PDF default11454байта, lossless19050байт. Это поддерживает вывод про default JPEG export settings движка и цену lossless; root отдельно исправляет policy и запускает final controls.

## Кандидат, не подтверждённый реальным native-случаем

WebDocument-only proof + nativeTextDocument=True расширяется Registry до Writer для выбора PNG, но старая preservation scope смотрит raw services и может вернутьNone. Mock boundary воспроизводит отсутствие page receipt; на реальном Linux proof включает Writer и защищён. Нельзя выдавать mock за подтверждённую потерю текущего Windows LO. Код по этому кандидату не менялся.

## Проверки и артефакты

- `run_before_fix_checks.py` импортирует exact660b470 app_backend из git archive и запускает три новых behavioral метода. Итог:5 assertion failures,0errors. `new-behavioral-tests-on-660b470.log`, `before-fix-execution.json`.
- `presentation-targeted.log`:93PASS, включая8новых presentation regressions.
- `legacy-targeted.log`:52PASS, включая15новых legacy regressions. HTTP тесты выполнены вне sandbox после socket EPERM.
- `after-fix-evidence.json`: старый реальный native CSV2→1 и оба presentation checker теперь SAFE_REJECTION.
- `probe_native_boundary.py`, `native_boundary/evidence.json`: фактические native controls на Linux LibreOfficeDev26.8alpha, не Windows26.8.2.
- `probe_presentation_overlap.py`, `presentation_overlap_fixture/`: исходное validator false success.
- `probe_web_preservation_scope.py`, `web_scope_fixture/`: только mock candidate.

Воспроизводимые source/output fixtures созданы нами. Исторические пользовательские документы не использовались.

## Остаточные границы

- Legacy XLS/XLSB и другие бинарные книги не получили нового парсера количества листов; при отсутствии подтверждённого XML inventory статус not_checked.
- Совпавшее число файлов не доказывает правильные значения, формулы, порядок, оформление или отсутствие одинаковых копий одного листа. Полный content audit не заявляется.
- Hidden, empty и chart tabs включаются в заявленное число исходных листов. Пустой/неоднозначный выход не сертифицируется. В legacy API не выбирался один активный лист.
- Проверка XML ограничена64MiB/1m nodes/20k sheets, исчерпание считается incomplete, а не успехом. Парсер удаляет завершённые узлы и проверяет отмену; отдельный native export не запускается.
- Для поддержки shared helper один существующий test-loader изменён с AST-only subset на полный trusted WORKER_SOURCE при __name__ != __main__; все assertions сохраняются, UNO/native main не запускается.
- Коммитов, push, merge и выпусков этим агентом не выполнялось. Root выполняет полный suite и Windows CI после объединения изменений.
