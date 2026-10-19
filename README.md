# A.I.O — All in One помощник

**Новая чистая основа проекта (перенос 19 октября 2026).**

Сохраняем существующую архитектуру оболочки A.I.O, пользовательский интерфейс, светлую и тёмную темы, вкладку **«Объединение Excel»** и готовую визуальную вкладку **«Конвертер файлов»**.

## Архитектура

**Интерфейс A.I.O → A.I.O Converter Guard v0.1 → LibreOffice.**

Оболочка использует Qt WebEngine и HTML/CSS/JS. Объединение Excel подключено отдельно и не зависит от конвертера. LibreOffice используется как внешняя установленная копия; Guard поставляется отдельным интеграционным патчем.

Старые конвертерные backend/allowlist/Advanced/маршруты **не перенесены**. В этом патче подключена первая основа Guard: прямой экспорт **DOCX, DOC, ODT, RTF, XLSX, XLS, ODS, PPTX, PPT, ODP → PDF**, последовательная очередь, независимые ошибки файлов, отмена, сохранение и журнал этапов. ADVANCE остаётся замороженным. Другие возможности LibreOffice подключаются отдельно; этот список описывает покрытие адаптера.

LibreOffice устанавливается отдельно. Можно указать путь к `soffice.com` (Windows) или `soffice` через переменную `AIO_LIBREOFFICE`. При отсутствии движка Excel продолжает работать, а Guard сообщает `ENGINE_NOT_FOUND`. Готовый PDF ожидает сохранения до сброса задания или закрытия приложения; истечение времени ожидания пользователя не удаляет результат.

Контракт API, проверки и границы первой версии: [docs/CONVERTER_GUARD_FOUNDATION.md](docs/CONVERTER_GUARD_FOUNDATION.md). Первая отдельная Windows-сборка переноса UI/Excel остаётся привязана к своему старому SHA без Guard; этот патч не объявляется проверенной Windows-сборкой.

Объединитель Excel теперь не зависит от конвертера. Его прежняя логика объединения, стили и проверки сохранены; проверки XLSX вынесены в самостоятельный файл `app_backend/xlsx_integrity.py`.

## Запуск в исходниках

Python 3.11+, пакет Qt WebEngine (PySide6), `openpyxl`.

```bash
python -m pip install -r requirements.txt
python launcher.py
```

Тестирование без GUI:
```bash
python -m unittest discover -s tests -p 'test_excel.py' -v
python -m unittest discover -s tests -p 'test_clean_foundation.py' -v
python -m unittest discover -s tests -p 'test_converter_guard.py' -v
python -m unittest discover -s tests -p 'test_converter_guard_http.py' -v
node --test tests/test_converter_guard_bridge.cjs
```

Это исходники в **Draft PR миграции**, не Windows-релиз и не подтверждённая сборка на NIKITA.

### Точная исходная контрольная точка

`jumonja-maker/All-in-One-` @ `f342a28086e4f9b59dc086a9cd52d4875d9a7cb8`.

Правила и ограничения переноса: [docs/MIGRATION_SCOPE.md](docs/MIGRATION_SCOPE.md).

В полноразмерном качестве исходные изображения Эльвы и логотипа пока находятся в старом приватном репозитории; в новой ветке сохранена аутентичная уменьшенная WEBP-версия Эльвы и фирменные SVG. Полный перенос крупных PNG требует отдельного доступа к бинарным файлам и пока не считается завершённым.

Старый репозиторий и Advanced оставлены без изменений.
