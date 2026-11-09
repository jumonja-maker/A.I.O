# A10 — wheel hashes, SBOM, SCA и обновление Pillow

Дата: 30.10.2026. Исходная база QA `ba920941d085679be0105a82659a9dcb849fe0e3`, отдельный PR #94 в QA. MAIN/Release не меняются.

## Проблема и результирующее поведение

Ранее pipeline проверял точные версии, но не закреплял реальные wheel-байты (`wheelHashesVerified=false`), не создавал SBOM и не выполнял SCA. Начатая независимая SCA обнаружила у закреплённого **Pillow 12.3.0** 59 OSV записей, включая дубли GHSA/PYSEC. Это **не 59 доказанных уязвимостей A.I.O**: использование конкретных уязвимых API приложения отдельно не воспроизводилось. Официальные release notes Pillow 13.0.0 подтверждают исправления; версия 13.0.0 поддерживает Python >=3.11. Принят security update, без удаления проверок сохранности и без сокращения маршрутов конвертации.

## До / после

| Свойство | До | После |
|---|---|---|
| Точные версии и lock-граф | Проверялись | Сохранены, Pillow поднят до 13.0.0; development range >=13,<14 |
| Реально скачанные wheel-байты | Не закреплены | SHA-256 30 Windows/Linux wheel/profile пар для 15 пакетов; независимо сверены с PyPI release metadata |
| Получение пакетов | Обычная pip-установка по версии | `--require-hashes --only-binary=:all:` |
| Установка | Через индекс | Только проверенная новая local wheelhouse, `--no-index --force-reinstall`, те же хеши |
| Фактически использованный источник | Не удостоверен | pip report должен содержать все и только ожидаемые локальные wheel URI, версии и SHA-256 |
| SBOM | Отсутствовал | Детерминированный CycloneDX 1.6, purl и SHA каждого wheel; отдельные runtime/build SBOM не перезаписывают друг друга |
| SCA | Не выполнялась; Pillow 12.3.0 даёт совпадения | OSV batch по точным pins на каждой фазе; findings, неполный ответ и отсутствие сети блокируют CI |
| Замороженный EXE | Версионный manifest | Требует подтверждённые wheels, SBOM и SCA report, включает все три source-bound документа |

Профиль: **CPython 3.11, x86-64 Windows/Linux**. Это воспроизводимый профиль получения Python-зависимостей QA, не доказательство побитовой воспроизводимости готового установщика.

## Проверка байтов

`qa_wheels.py` проверяет SHA исходников, версию Python, весь version lock-граф и согласованность byte lock. Новая фазовая директория принимает только binary wheels с зафиксированными хешами. Независимый потоковый SHA-256, верхнеуровневый wheel METADATA и точный inventory проверяются до и после установки. Чужая копия/URL, пропущенный пакет, другая версия/хеш не дают квитанцию. `verify_qa_dependency_lock.py` повторяет проверки до создания build manifest. Без квитанции прежний verifier честно оставляет `wheelHashesVerified=false`; такой manifest не проходит QA EXE specification.

SBOM описывает **Python wheel distributions**. Вложенные vendored библиотеки setuptools и нативные компоненты Qt/LibreOffice/системных DLL ещё не развёрнуты. SBOM и SCA имеют разные scope и самостоятельные квитанции.

## SCA и Pillow

[Полный исходный batch-ответ](evidence/a10/pillow-12.3.0-osv-before.json) привязан к опубликованному source SHA `912969ecdadb7251059f9d4bda959e21edf940de` и точным lock hashes. В 15 объявленных distributions совпадения получены только для Pillow 12.3.0. Проверка кандидатного набора с Pillow 13.0.0 дала 0 совпадений; она не выдаётся за final exact-SHA CI. Pipeline нового SHA должен заново получить и проверить актуальный результат.

`qa_sca.py` не считает timeout/неполный/постраничный ответ успехом. Отчёт содержит запросы с версиями, исходный SHA, lock hashes, дату, источник и ответы OSV. Verifier пересчитывает статус из raw response, поэтому подмена status на PASS при наличии advisory не проходит. Build manifest и EXE связывают SHA SCA-файла с текущими исходниками. Сетевой отказ — INCOMPLETE и ненулевой exit code, не скрытый skip.

Ограничение: отсутствие совпадений означает отсутствие записей OSV **для проверенных объявленных версий на момент запроса**. Это не доказательство отсутствия уязвимостей в приложении/вложенных компонентах и не sandbox LibreOffice.

## Проверки и неудачные этапы

- 30 реальных wheel/profile пар проверены; после обновления заново скачаны и сверены оба Pillow 13.0.0 wheel. Остальные 28 хешей неизменны.
- **31 целевой QA-тест PASS**: 11 прежних lock-контрактов, 13 новых wheel/SBOM/install регрессий, 7 SCA-регрессий. Дополнительно **5 required gate тестов PASS**, **7 PCX/RAS/raster fidelity тестов PASS** на Pillow 13.0.0. Ограничения и платформенные исключения не расширены.
- [Первый CI A10 #41121712204](https://github.com/jumonja-maker/A.I.O/actions/runs/41121712204) на `912969e`: Linux wheel download/install/manifest **PASS**, source suite **FAIL** (1439 discovered, 1433 PASS, 5 Windows N/A, 1 ERROR). Причина: существующий test_required_qa_gate искал прежнюю прямую pip-команду. Тест обновлён с сохранением и усилением порядка **check-locks → hash install → SCA → attestation**; gate не ослаблен. Этот FAIL сохранён как факт.
- Первый cross-platform Linux download указал один manylinux_2_34 tag и не выбрал совместимый Pillow wheel с более ранним tag. Повтор с полным набором совместимых tags дал все 15 файлов; pins не ослаблялись.
- Первичная генерация увидела вложенные vendor dist-info setuptools; проверка уточнена до единственной верхнеуровневой METADATA, отдельная регрессия сохранена.
- В рабочих download-каталогах обнаружились временные файлы. Строгий inventory не ослаблен: подтверждённые wheel-байты отдельно сверены в чистых каталогах. Лишние файлы в CI приводят к отказу.
- Локальный Python 3.12 не выдан за installation acceptance 3.11; source GUI на локальном host ограничен отсутствующей libXtst.so.6. Окончательный hosted Windows/Linux + native LO26.8.2.2 + EXE + общий gate нового SHA отслеживается отдельно и заранее не объявляется PASS.

## Незакрытая часть A10 / G04

Остаются actual hosted feature/merged SHA, расширение SBOM/SCA на вложенные native/vendor компоненты, единая цепочка LibreOffice MSI/EXE/source для выпуска, повторные воспроизводимые бинарные сборки и установленный Windows G01/G04. **A10 ЧАСТИЧНО**. Installer/Release и G01–G05 не закрываются этим пакетом.

Канонический мешок обновлён. Google Drive не подключён: архив 00AIO **BLOCKED**, не обновлялся. Исходный FULLTEST 200/5723/1079 не запускался.

## Первичные источники

- https://pip.pypa.io/en/stable/topics/secure-installs/
- https://pypi.org/pypi/Pillow/13.0.0/json
- https://github.com/python-pillow/Pillow/blob/main/docs/releasenotes/13.0.0.rst
- https://api.osv.dev/v1/querybatch
