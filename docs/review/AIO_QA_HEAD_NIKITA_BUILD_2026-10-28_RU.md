# 00AIO — QA-only Windows EXE/installer from merged QA HEAD (28.10.2026)

## Задача

Собрать не релиз, а изолированный Windows-кандидат из уже объединённой рабочей QA-ветки после PR #79. Продуктовые исходники при этом не изменяются и main не затрагивается.

- QA source SHA: `d57e4cf75cc0f46f0bfbfa1da01a23d22018c3dc`.
- QA Git tree SHA: `bc48522b79c5365d3fc2f6f65effec43f824d3b0`.
- Runner: только `NIKITA`, метки `self-hosted, Windows, X64, local-d-manual`.
- Trigger: ручное назначение метки `aio-qahead-build-approved-once` PR владельцем `jumonja-maker`.
- Checkout: строго указанного source SHA, без сохраняемых credentials и без использования mutable PR head.
- Никаких GitHub-hosted минут и online package install. Внешний LibreOffice не заменяется, пользовательские файлы не удаляются.

## Последовательность QA

1. Убедиться, что NIKITA — неадминистративный `aio-runner`, временные и рабочие пути лежат на D:, место на C: и D: достаточно.
2. Проверить исходный Git tree SHA и уже установленное доверенное Windows окружение Python 3.11/PySide6; проверить Node, полный локальный LibreOffice и Inno Setup.
3. Распаковать и независимо сверить SHA офлайн-зависимостей `tools/qa/guard_v17_test_dependencies.py` только в новой папке `RUNNER_TEMP`.
4. Выполнить все применимые Python/Qt/DOM source regression тесты `tools/qa/guard_v17_selfhosted_acceptance.py`.
5. Выполнить целевой реальный Guard/LibreOffice `guard_remaining_bag_stress3.py`. Это НЕ исторический пользовательский FULLTEST.
6. Собрать `AllInOne.spec` через заранее установленный PyInstaller, проверить QtPdf, LibreOffice, и `src/guard-comparison-viewer.js` и `index.html` в пакете.
7. Сформировать `AIO-Guard-QA-20261028-win-x64.exe` и `AIO-Guard-QA-20261028-PORTABLE.zip`, SHA256SUMS, BUILD-STATUS, CRC ZIP.
8. Выполнить реальный smoke `guard_v19_frozen_acceptance.py` над созданным EXE и только после успеха копировать файлы в **новую**, ранее не существовавшую папку `D:\AIO-Lab\AIO-QA-Dist\qa-head-20261028-RUNID` с независимым SHA256 readback.

## Границы подтверждения

Успешная упаковка и frozen worker acceptance **не доказывают** проверку установленного приложения с реальным пользователем, полноту GUI-сценариев и сохранение всех исходных документов. Windows EXE не устанавливается, не публикуется, не мержится в main, не проводит FULLTEST 680 и не объявляется готовым к выпуску.

Изменения этого PR: только поддержка одноразовой QA-сборки на NIKITA и этот отчёт. До появления реального run выводы о сборке считаются `NOT_RUN`.

## Разбор первого Windows build run 40731289219 (FAIL)

Источник: [GitHub NIKITA #40731289219](https://github.com/jumonja-maker/A.I.O/actions/runs/40731289219), workflow HEAD `8d9fdeaaa6cfd495bb4799337499801db11dc6c7`, source SHA зафиксирован прежний `d57e4cf...`.

- Полный Windows source/Qt/DOM прогон завершился `PASS: 1207 tests / 10 platform-not-applicable / 0 FAIL/ERROR`.
- Все восемь предыдущих native stress3 этапов получили PASS: `native-profile`, `all-sheets`, `dif-regression`, `text-id-regression`, `dbf-jpeg`, `tiff`, `html`, `session`.
- Точный останов: `viewer-100: child failed, see retained evidence`, затем `NATIVE_STRESS3=failure`. Логи QtWebEngine содержат `Context lost during MakeCurrent`, `SharedImageStub: context already lost`, сбои D3D/Skia.
- Сторожевой тест не выключен. Установщик и ZIP законно `SKIPPED`, отчёты не переименованы в PASS.
- Корневая причина по имеющимся логам не доказана на 100% (внутренний файл `viewer-100/evidence.json` не опубликован); **наблюдаемая средовая регрессия**: в исходном успешном [NIKITA #40716613385](https://github.com/jumonja-maker/A.I.O/actions/runs/40716613385) `QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu --disable-gpu-compositing` был установлен глобально, а в первом новом запуске он был только внутри этапа `source_qa`, но отсутствовал у native stress3 viewer children.

### Точечный workflow hotfix

- Для всех шагов и потомков процесса, включая viewer-100/150, глобально заданы `QT_OPENGL=software`, `QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu --disable-gpu-compositing`, `AIO_QT_SAVE_QA=1`. Это относится исключительно к CI, не к продуктовой настройке.
- Перед полным Windows source/native stress3 добавлена самостоятельная **реальная** `guard_before_after_b19_b25_acceptance.py` с native Qt mouse/keyboard, 4K screenshot, точными пикселями, двумя темами, fullscreen, APNG и обязательными пятью проверками + двумя изображениями. Никакой fake PASS/skip.
- На неуспехе ранний этап печатает только ограниченную текстовую причину из `evidence.json` и отклоняет сборку, без передачи скриншотов/файлов пользователя.
- Исходный Native stress3 viewer-100 и viewer-150 остаётся обязательным, затем frozen EXE QA, инсталлятор и SHA256 readback.
- Не отключать тест и не обходить safety gates ради готового EXE.

После фикса допускается только новый отдельный запуск на NIKITA; результат пока **NOT_RUN**. Первый failed run остаётся в истории.
