# 00AIO — QA frozen PNG preview contract, 28.10.2026

## Подтверждённый отказ

- [NIKITA run 40738324244](https://github.com/jumonja-maker/A.I.O/actions/runs/40738324244), окно 42m22s, workflow commit `46574c71052f625b702ea3561ef37d7c6169f1f9`. Исполняемый продукт упакован **из точного QA SOURCE** `d57e4cf75cc0f46f0bfbfa1da01a23d22018c3dc`, tree `bc48522b79c5365d3fc2f6f65effec43f824d3b0`.
- Windows source regression, viewer-100/150, 434-decision native targeted stress3, PyInstaller и Inno Setup имели SUCCESS.
- Два файла **реально созданы, но не были архивированы**, так как обязательный frozen QA step упал:
  - Setup: `AIO-Guard-QA-20261028-win-x64.exe` — 364230023 bytes, SHA256 `d7690fdbaee12ab554abb9704d9242df14a537c12027eaf4d3845128bbccdb98`.
  - Portable ZIP: `AIO-Guard-QA-20261028-PORTABLE.zip` — 539877533 bytes, SHA256 `07937b67ce13b8b1540b8e7871635d88c04df26e5d8736bf59963f98249710dd`, ZIP CRC PASS.
- Проверка `guard_v19_frozen_acceptance.py`: text/PDF/QtPdf/asset SHA steps SUCCESS; `png-preview` returned exit 0, но тест упал с `AssertionError: Preview exceeds the production image bounds` ещё до анализа PNG metadata.
- Причина в **тесте**, не в конвертере: `png_info` использовал PDF-лимит 1400×1800 для всех PNG, хотя `comparison_worker.py/image_preview` поддерживает настоящие 8192 пикселей по стороне и 12 млн пикселей по площади. Искусственный 2400×1200 RGBA — всего 2.88 млн пикселей и не должен уменьшаться до 1400×700.

## Тестовый патч (не продукт)

- `png_info` получает явные `max_width`, `max_height`, `max_pixels` и продолжает проверять PNG signature, CRC, IHDR, IDAT, IEND, deflate и целостность каждой строки.
- Реальные PDF-предпросмотры остаются в строгом 1400×1800 лимите.
- Image preview использует исходный лимит **8192 / 12 млн**; дополнительно требует полное исходное разрешение 2400×1200, alpha RGBA, совпадение размеров с `metadata.renderedWidth/Height` и исходной геометрией.
- Регрессионные unit-тесты отдельно доказывают сохранение исходного разрешения и отбраковку пакетов с корректным CRC, но превышенным лимитом по оси/площади, а также отбраковку повреждённого CRC.
- Продуктовый `app_backend/converter_guard/comparison_worker.py`, настройки качества, Guard, исходники, результаты и `main` не изменены.

## Повторная приёмка

Владелец разрешил самостоятельный запуск на self-hosted NIKITA. Новая независимая проверка должна найти **ровно** пакеты из предыдущего run и сравнить их SHA256, проверить сохранённый source/tree и отсутствие reparse points, CRC архивной копии и EXE внутри ZIP, затем запустить *исправленный* тест настоящего frozen EXE. Только после `frozenVerified=true`, `fixturesUnchanged=true` и всех обязательных проверок допустимо скопировать результаты в **новую** папку QA archive с SHA256 readback. Ни один предыдущий пакет не перезаписывается. Если исходных файлов больше нет или что-то изменилось, восстановление должно безопасно завершиться FAIL и потребуется новая сборка.

Это не пользовательский installed EXE test, не FULLTEST на 680 оригиналах и не релиз. Старый failed run сохраняется в истории.
