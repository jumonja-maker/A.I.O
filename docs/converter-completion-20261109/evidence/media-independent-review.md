# Независимый review media completion

Две найденные ошибки/границы исправлены владельцем media до завершения review:

1. Новая проверка DPI для статичных JPEG/BMP сравнивала разрешение всех страниц исходного TIFF. При двух страницах и корректном DPI первой JPEG/BMP-страницы второй source-DPI не имел пары и приводил к ложному `MEDIA_DPI_LOSS`. Ограничено сравнение представленным первым кадром/страницей, при этом `pages`/`animation` loss receipt сохранён. Неверный DPI первой страницы по-прежнему блокируется. Независимая проверка `dpi-static-repro.json` сравнивает b7216d1, исходный completion-кандидат без новой первой-страницы границы и исправленный результат: PASS / MEDIA_DPI_LOSS / PASS для JPEG и BMP.

2. `encode_native_gif_pages` проверял имена и длину списка, но мог переписать противоречивые schema/pageCount/page ordinals в новый GIF manifest. Добавлены точные int schema=1, pageCount=N и номера [1..N], с отказом до публикации GIF. Новый regression требует сохранности PNG и исходного manifest при ошибке.

Дополнительно независимо пройдена отмена в момент публикации: после появления первого GIF устанавливается cancel, следующий шаг даёт `CANCELLED`; опубликованные GIF удаляются, все native PNG и manifest остаются байт-в-байт прежними (`gif-boundaries.json`). Проверены условия коллизий, отдельность source/output и проверки PNG-контейнера перед точным GIF encoder; готовый неполный GIF-поднабор не найден.

Границы PCD/TGA/PICT просмотрены: PhotoCD ограничен подтверждённым Base raster 768×512 и ориентациями 0/90/270, с decodedResolution в профиле; 180°/неполный Base остаются вне новой Qt/Pillow ветви. TGA требует бинарную декларацию плюс живой Qt decoder и совпадающий canvas; source bytes не записываются. PICT исключает обычный Draw: необходим настоящий native import filter, объявленный QuickDraw frame, один неподвижный/нескошенный/необрезанный GraphicObjectShape; output резервируется xb и удаляется при исключении.

Итоговый независимый focused suite: 13/13 PASS, без skips (`review-regressions.log`). Новых нерешённых блокирующих регрессий в просмотренном коде не обнаружено. Настоящий Windows UNO GraphicProvider для PICT и native PNG→GIF pipeline этим Linux-review не сертифицированы: агент media сохранил BLOCKED в native отчёте, обязательный Windows driver/gate остаётся необходимым. Исторический FULLTEST, все разрешения PhotoCD, сложные PICT/цветовые профили и абсолютная визуальная эквивалентность вне доказанного объёма.

Хеши просмотренных файлов: `review-manifest.json`.
