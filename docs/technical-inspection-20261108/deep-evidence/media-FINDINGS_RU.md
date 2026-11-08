# Deep media audit — новые подтверждённые случаи и исправления

База воспроизведений: `660b47002672a3ad86e4ce8b1cd7c514df178295`.
Production-файлы изменены без коммита; основной агент управляет объединением в PR103.

## DM-01 — CMYK/Gray ICC на RGB-пикселях (P1)

Валидный CMYK JPEG с ICC Artifex из установленной системы давал после
PNG/APNG/WebP/TIFF пиксель `(215,107,0)` с профилем, объявляющим **CMYK**,
хотя сам растр уже RGB/RGBA. LittleCMS не может построить преобразование
такого результата, Qt игнорирует профиль, но проверка AIO возвращала
`checked_subset`, `expectedLosses=[]`. Перцепционный исходный oracle давал
`(213,129,36)`; relative-colorimetric — `(214,132,49)`.

Причина: AIO менял число каналов через `QImage.convertToFormat`, но сохранял
старый ICC. Это не выполнялось через LibreOffice. Gray ICC таким же образом
попадал в RGB APNG/WebP/TIFF; Qt JPEG-путь терял Gray ICC.

Исправлено: валидные CMYK/Gray профили преобразуются существующим Qt CMS в
sRGB **до** перевода в RGB/RGBA. RGB/Display P3 остаются в исходном пространстве.
Сохраняется 16-bit pixel format, когда исходный декодер его предоставляет.
Некорректный/неподтверждённый встроенный ICC и CMYK без подтверждённого профиля
явно отклоняются при преобразовании модели. Новых runtime-зависимостей нет.

Регрессии используют независимый Pillow/LittleCMS, intent RELATIVE_COLORIMETRIC;
допуск два уровня 8-bit канала объясняется различиями интерполяции/округления
двух CMM. Для восьми контрольных декодированных CMYK-образцов фактическая
разница Qt/LCMS была не более одного уровня.

ICC fixtures офлайн: CC0 `CGATS001Compat-v2-micro.icc` из установленного
pdfjs-dist 5.6.205 (8464 B) и Zlib `Gray.icc` из Debian icc-profiles-free (420 B).
Оригинальные лицензии, авторство, хеши и provenance сохранены в
`tests/data/deep_media_profiles/README.md` и соседних license-файлах.

## DM-02 — физический размер/DPI (P2)

* PNG 300 dpi → APNG терял pHYs и становился неявными 96 dpi.
* APNG 300 dpi → TIFF сохранял кадры, но compositor создавал холст 96 dpi,
  поэтому при одинаковом числе пикселей физический размер увеличивался в 3,125 раза.
* JPEG с orientation6 и разными X/Y DPI вращал пиксели, но не менял местами
  плотности X/Y.
* PDF, отрисованный при 144 dpi, при композиции JPEG на белом холсте получал
  метаданные 96 dpi, хотя предупреждение продолжало утверждать 144 dpi.

Исправлено: pHYs сохраняется в APNG; compositor переносит DPM на холст;
известные TIFF pageDpi восстанавливаются при чтении; вращение меняет оси DPI;
белый JPEG-холст PDF наследует плотности исходного отрисованного кадра.
APNG с одинаковыми пиксельными размерами, но различным физическим масштабом
страниц теперь явно отклоняется: один глобальный pHYs это не представляет.

Дополнительно независимый postcondition сверяет **явно заданный** физический
масштаб PNG/APNG/TIFF и отказывает при потере. Это не сертификация отсутствующих
метаданных и не обещание сохранения любых EXIF/печатных параметров.

**Осталось:** manual WebP/GIF writers не сохраняют DPI; физические метаданные
этих целей не исправлялись. Политика aspect ratio с неопределённой единицей
и все нестандартные TIFF/EXIF extensions не сертифицированы. Нельзя обобщать
этот DPI-fix на любые метаданные и все форматы.

## DM-03 — JPEG EXIF после SOF (P2)

В валидном JPEG APP1 EXIF расположен после SOF, до SOS. Pillow и Qt видели
orientation6 и показывали изображение 2×3; AIO прекращал metadata scan на SOF,
получал orientation1 и создавал PNG 3×2 с `checked_subset`.

Исправлено в рамках чтения присутствия ICC: header scan продолжается до SOS,
без чтения entropy данных в быстром режиме. Так поздние EXIF/ICC учитываются
до декодирования. Существующий record budget и cooperative cancellation остаются.
Это не заявление о поддержке произвольных нестандартных тегов после entropy.

Проверка совместимости отдельно обнаружила прежний intentional source-only
контракт: размеры уже читаются из SOF даже при дальнейшем усечении источника,
чтобы нативный декодер мог попытаться восстановить его. Этот контракт сохранён
через `metadataComplete=false` только для fast source probe. Строгий
`validate_container=true` по-прежнему отклоняет такой JPEG; Qt cross-container
не использует неполные метаданные для публикации результата и явно отказывает
до создания output. Валидный late-EXIF JPEG продолжает сканироваться до SOS.

## DM-04 — известные expectedLosses скрыты от пользователя (P2)

Альфа, animation/pages semantics, bit depth и GIF representation limits
оставались во внутренних resultChecks и не становились предупреждениями.
Более того, frontend выбирал `qualityText || warnings`, поэтому любой объект
quality скрывал имеющиеся предупреждения даже при явной потере.

Исправлено: известные потери преобразуются в существующие warning codes и
русские WARNING_TEXT; core уже переносит их в status/отчёты. GUI показывает
и качество, и предупреждение одновременно. Ошибка остаётся первичным текстом;
сообщения выводятся как textContent, не HTML. Для animation→TIFF сказано про
страницы без времени, а не ошибочно про один кадр; GIF-текст описывает пределы
формата и не утверждает, что строгий writer принудительно квантовал цвета.
Текст 144-dpi PDF warning обобщён с TIFF на растровые изображения.

Потери DPI WebP/GIF не добавлены в expectedLosses этим followup; это остаётся
явным ограничением документа. Legacy прямой convert_path не получает эти
Guard-adapter warnings автоматически.

## Проверки и источники свидетельств

* `before-660-final-tests.log`: 13 новых тестовых методов против исходного
  app_backend660 и guard-queue.js660, 22 ожидаемые падающие подпроверки.
* `verified-media-targeted.log`: 83 targeted tests PASS, включая первые 13 новых,
  действительные Qt codecs, независимый LittleCMS, pypdf/QtPdf child,
  существующие frame/media/preservation/defaultPDF/TI tests и DOM lifecycle.
* Окончательный `verified-media-recovery-compatibility.log`: **95 PASS**, включая
  весь OutputIntegrityV18 и новый 14-й тест source-probe/strict-output/no-Qt-ready.
* `warnings-before-fix.log`: четыре действительных Qt-преобразования имели
  expectedLosses, но не имели warning_details.
* `dom-warnings-before-fix.log`: UI воспроизводимо скрывал предупреждение
  при quality.status=risks_found.
* `probe_colors.py`, `probe_dpi.py`, `probe_late_exif.py`: сохранённые отдельные
  воспроизведения, small generated fixtures и исходные JSON observations.
* `before-source.json`: exact base/source snapshot provenance.

Промежуточный `targeted-final.log` содержит ошибку **имени запрошенного тестового
модуля** в команде harness, не дефект AIO. Окончательный авторитетный targeted
результат — `verified-media-targeted.log`. Старые exploratory JSON могут быть
перегенерированы после исправления; исходные failure-логи и exact660 baseline
сохраняют отрицательное свидетельство.

Полный suite, Windows CI и LibreOffice native matrices этот агент не запускал.
Местный Qt не имеет TIFF decoder plugin; TIFF output всегда независимо прочитан
Pillow/LittleCMS, а Qt-reopen дополнительно проверяется там, где codec доступен.
Окончательный Windows-прогон и полный suite выполняет основной агент.
