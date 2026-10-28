# 00AIO — оставшийся мешок после B28/B29

Статус отчёта: **PENDING**, совместная native stress3 ещё не выполнена. Это рабочий checkpoint, не разрешение на merge.

База QA: `e80cdb84b90f2737b492185a3eb22586570c9f8c`. Main: `2bab2d0c31fe84a0d07404a33219dd5bdad834b7`. Ветки не изменялись; пользовательские испытания не удалялись. FULLTEST, release, EXE build и merge не запускались.

## Отдельные PR

| Этап | PR | Проверяемый source SHA | Tree | Состояние |
|---|---|---|---|---|
| A | [#67](https://github.com/jumonja-maker/A.I.O/pull/67) | `78e2c68f12d6297efa92d2c0f46f948ffb6466bb` | `9f71526cb76934e927081287d838066aef634b2d` | PASS |
| B | [#68](https://github.com/jumonja-maker/A.I.O/pull/68) | `601a73056017b55096159f236aae48ba05b21577` | `51cfa0ea0ac5673ddbee0dab25696e5b54253614` | PASS |
| T | [#70](https://github.com/jumonja-maker/A.I.O/pull/70) | `2e3e245091ab1aee2780f8036d8ff7d890f78074` | `9e63f707be6785af31da3b8dd8a594448f6f8cce` | PASS |
| H | [#71](https://github.com/jumonja-maker/A.I.O/pull/71) | `b129d2cc22d0826cf2ba3d866ddc330020a2a126` | `5bd3d5f3ac9564bf47e70d9145d6071696cb443a` | PASS |
| C | [#69](https://github.com/jumonja-maker/A.I.O/pull/69) | `6b29e4340485378e1186e885cf1875f5adac55ea` | `16c6b8ff1ed39e549ad28f577416e85746f9a151` | PASS |
| D | [#72](https://github.com/jumonja-maker/A.I.O/pull/72) | `56991581b23da0695981d7106c734d7954af016f` | `5b8545dd4044f5178343a08d94fed7352712f4c4` | PASS |
| E | [#73](https://github.com/jumonja-maker/A.I.O/pull/73) | `d6ed1c3b6217d1a931fbcaf92fb2bd00e9e515ee` | `cbd7f838c7b79f52298ed7aa006f7066e3ff4717` | PENDING |

## Что реализовано

- A / B30+B34: native export каждого листа, безопасные ordinal names, sourceSheet manifest, отдельные SHA/readback, честные conversion/save/quality поля. CSV не обещает редактируемые формулы и оформление.
- B / B13+B31: CP1251 C9 подтверждается по содержимому/структуре; потоковая проверка всех записей сохраняет корректные DBF больше 1 МиБ. JPEG aliases привязаны к настоящему установленному JPEG filter с decode/dimensions/RGBA controls.
- T / B32: свежий Qt TIFF reader на каждый подтверждённый IFD; сохранены все страницы и ограничения; independent PNG/APNG/WebP/PDF pixel checks.
- H / B33+B14: текстовый Impress HTML допускается только после native shape profile; graphics/unknown routes объясняются заранее. Сохранён реальный одностраничный Draw XHTML export. ORP без настоящей Base fixture остаётся not_tested.
- C / B16–B18+B23–B24+B26–B27: постоянные три кнопки, правильный unsaved count, безопасная корзина и Close Да/Нет, сохранение/восстановление, необязательная выбранная диагностика, блокировка поздних completion после Close.
- D / B19–B22+B25: две инертные панели, реальное разрешение/страницы/кадры, zoom/pan/fullscreen, доступные фильтры включая ошибки конвертации, default problems, HTML/text/grid и verified sheet mappings. Проверка реальных Qt painted pixels обязательна.
- E / B10+B15: новый целевой корпус, 24 native OOXML положительных контроля, точные B28/B29 и матрицы features, 12 parser/identity controls; null failure metrics до полного PASS.

## Пробелы доказательств

Оригинальный ZIP 680 файлов и дополнительные master/handoff/stress2 Markdown не были доступны. Прочитаны основной ТЗ, доступный XLSX/PDF audit и история PR #56/#59/#60/#62/#63/#65/#66 с отчётами v20–v23. Точный исторический baseline fidelity не воспроизводился; новые синтетические матрицы не выдаются за его повтор.
Настоящая Base ReportDefinition, macro-bearing positive corpus и installed EXE/manual acceptance остаются not_tested. Проверка source Qt на runner не заменяет пользовательскую EXE-приёмку. Полный descendant PID inventory отдельно не собирался; CLI не разделяет import/export фазы.
B08/B11/B12 (скорость) отложены владельцем. Новые функции ADVANCED не разрабатывались. Исторические B01/B02 и соседние защиты не отключались; новые GIF/alpha контроли требуют правильного отказа.

## CI

NIKITA source/native gates указываются отдельно от hosted clean-foundation и telemetry. Последние имеют failure до выполнения шагов; логи недоступны (404 BlobNotFound), причины annotations этим подключением не раскрыты. Эти hosted jobs не объявляются PASS. Их тестовые контракты входят в полный NIKITA source suite; runtime BLACK.BOX не включался.

## Окончательная приёмка

Шесть продуктовых этапов A/B/T/H/C/D имеют отдельные принятые source gates. PENDING остаётся только новый интеграционный source SHA со stress3 и всеми соседними native/Qt/DOM/Excel регрессиями. Этот совместный PR предназначен исключительно для проверки, не для общего merge вместо независимых PR. Самовольного merge в QA не выполнять.

D: полный NIKITA run 40605908375 success, 1165 PASS + 10 N/A. B: run 40580027077 success, 1156 PASS + 10 N/A; 12 Git objects доказывают неизменность product/native генераторов относительно матрицы 40573845377 (сам старый run был failure из-за ошибочной ASCII fixture). В интеграции все нативные матрицы выполняются заново на одном SHA.

Локальный supplemental Linux: 9 POSIX/link/process controls PASS, 0 skips (execution session 68663), отдельный local snapshot, не Windows acceptance. Исходные SHA доступных ТЗ/PDF/XLSX — reference-provenance.json.

При локальном объединении один конфликт adapter.py разрешён явно: потоковая строгая DBF validation с общим RunControl сохранена перед нативным whole-workbook CSV preflight. Остальные изменения объединены с сохранением обоих наборов контрактов. Совместный source gate проверит итоговый код, а не сумму старых результатов.