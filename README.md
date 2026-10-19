# A.I.O — All in One помощник

## Этап: прямое подключение LibreOffice, без Converter Guard

Исходная оболочка A.I.O и Объединитель Excel сохранены. Вкладка **«Конвертер файлов X LibreOffice»** теперь использует новый самостоятельный транспорт `src/converter-bridge.js` и `app_backend/libreoffice_engine.py`, а не старую систему защиты, роутов и конвертерных обработчиков.

```text
A.I.O интерфейс (темы, Excel, конвертер)
  ├── Excel → собственный XLSX-движок (проверен пользователем)
  └── Конвертер → прямой API → LibreOffice Writer / Calc / Impress
                                  ↑
                        Converter Guard ещё не подключён
```

**Guard передаст GPT Work отдельно.** В этой ветке отсутствуют его код и механизмы блокировки. Advanced заморожен.

### Первая QA-матрица LibreOffice

| Входы | Первая группа выходов |
|---|---|
| DOCX, DOC, ODT, RTF, TXT, HTML, HTM | PDF, DOCX, DOC, ODT, RTF, TXT, HTML |
| XLSX, XLS, ODS, CSV | PDF, XLSX, XLS, ODS, CSV |
| PPTX, PPT, ODP | PDF, PPTX, PPT, ODP |

Это **направления прямого экспорта LibreOffice**, а не обещание, что все сочетания уже прошли пользовательский тест. Формат, совпадающий с исходным, не предлагается. PDF и растровые изображения как входы пока не подключены; не заявляем фиктивную поддержку.

Каждое преобразование использует отдельную временную папку и профиль LibreOffice, не изменяет исходные данные, проверяет результат и ограничивает время выполнения. Пакетные результаты выдаются ZIP. Лимиты первого этапа: 100 файлов, 20 МБ на файл, 50 МБ на партию, 100 МБ на результат.

### QA перед MAIN

1. Повторить автоматические тесты Excel и чистого интерфейса.
2. Запустить тесты прямого LibreOffice и реальные Writer/Calc PDF smoke на NIKITA.
3. Проверить Windows EXE с полным встроенным LibreOffice, native Save, повтор и макет обеих тем.
4. Сравнить результаты документов и таблиц; только после этого переносить проверенную версию в `main`.
5. После установки стабильной версии в `main` получить Guard от GPT Work и интегрировать отдельно.

### Запуск в исходниках

```bash
python -m pip install -r requirements.txt
python launcher.py
python -m unittest discover -s tests -p 'test_excel.py' -v
python -m unittest discover -s tests -p 'test_clean_foundation.py' -v
python -m unittest discover -s tests -p 'test_libreoffice_direct.py' -v
```

Для Windows QA-пакета используйте `AIO_LIBREOFFICE_ROOT` с **полным** проверенным дистрибутивом LibreOffice. PyInstaller откажется собирать Windows-пакет без него.

Исходный архив `jumonja-maker/All-in-One-` остаётся нетронутым. Новая ветка LibreOffice пока **не включена в main**, и установленная Windows-сборка этой ветки ещё требует проверки.
