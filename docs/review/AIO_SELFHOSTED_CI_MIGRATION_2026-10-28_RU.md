# A.I.O — миграция двух CI-контрактов на self-hosted (28.10.2026)

## Исходная точка

- Репозиторий: `jumonja-maker/A.I.O` (приватный).
- Базовый QA HEAD: `36ca685f7471a12d7697f59d7c8bb2b2deb53ca9`.
- Рабочая QA-ветка: `fix/guard-v182-matrix-progress-cancel-20261025` (PR #56).
- Main не изменяется; продуктовый код, Guard и BlackBox не затронуты.

## Подтверждённая диагностика

Просмотрены все 22 файла `.github/workflows/*.yml` на указанном QA HEAD.
Только два содержали `runs-on: ubuntu-latest`:

1. `clean-foundation-tests.yml`, [run 40722695283](https://github.com/jumonja-maker/A.I.O/actions/runs/40722695283).
2. `blackbox-telemetry-qa.yml`, [run 40722695285](https://github.com/jumonja-maker/A.I.O/actions/runs/40722695285).

Оба запуска получили `failure` до появления runner/тестовых шагов; каждый job был зарегистрирован и закрыт примерно за две секунды. Такой результат совместим с отсутствием доступных GitHub-hosted минут, но точная причина не доказана: job logs недоступны (BlobNotFound), сообщения биллинга не получены.

[Windows NIKITA run 40716613385](https://github.com/jumonja-maker/A.I.O/actions/runs/40716613385) действительно прошёл на self-hosted runner `NIKITA` с метками `self-hosted, Windows, X64, local-d-manual`. В существующих QA workflows также описан Linux runner `AIO-UBUNTU` с метками `self-hosted, Linux, X64, aio-vm-isolated`. Историческая действительность Linux runner подтверждена: [run 40197557880](https://github.com/jumonja-maker/A.I.O/actions/runs/40197557880) от 25.10.2026 имел SUCCESS в `linux-qa` на реальном `AIO-UBUNTU` с теми же четырьмя метками, а также SUCCESS Windows NIKITA. Его **текущий** online/offline-статус через доступный API не подтвердился.

## Исправления

Оба CI переведены на **`runs-on: [self-hosted, Linux, X64, aio-vm-isolated]`**.
Это сохраняет Linux-среду вместо переноса Linux-тестов на Windows NIKITA.

- Автоматическое использование персонального runner на каждом изменении PR **отключено**: `pull_request.types: [labeled]`, метка `aio-selfhosted-contracts-approved-once`, действие владельца `jumonja-maker`, PR автора `jumonja-maker` из того же репозитория. Допускается ручной `workflow_dispatch` владельцем не из `main`.
- Добавлены проверки имени `AIO-UBUNTU`, Linux, непривилегированного пользователя `aio-runner`, разрешённых `GITHUB_WORKSPACE` и `RUNNER_TEMP`, непересекающейся папки источника на каждый `run_id/run_attempt`.
- `actions/checkout` оставлен закреплённым на SHA; checkout без сохранения credentials, точная ревизия PR head (либо SHA выбранного manual-ref).
- Удалены шаги `actions/setup-python` и `pip install` из обоих заданий; используются только уже установленные Python 3.11, openpyxl (требуется только для clean; версия 3.1.2–3.1.x), Node. Если зависимостей нет — явный FAIL без сетевой установки или недоказанного PASS.
- `PYTHONDONTWRITEBYTECODE=1`, `PIP_NO_INDEX=1`, `PIP_NO_INPUT=1` и `TMPDIR=RUNNER_TEMP`. Тесты запускаются из отдельного checkout.
- Тестовые команды и названия исходных test suite сохранены. Релизов, загрузок артефактов, сборки EXE и удаления пользовательских файлов этот патч не делает.

## Проверка и ограничения

Статически проверены все 22 `runs-on` на базовом SHA, обоим изменённым файлам назначен Linux self-hosted, отсутствуют `ubuntu-latest`, `setup-python`, `pip install`, сохранены состав тестов, ограничения владельца и pinned checkout.

**Настоящий run на AIO-UBUNTU ещё необходим**. Нельзя приписывать тестам PASS до фактического запуска. Если runner offline или нужный Python/openpyxl отсутствует, статус останется queued/FAIL с явным сообщением. Старые исторические workflow failures не переписываются и не выдаются за прошедшие.

Для ручной проверки владелец может запустить каждый workflow из Actions через `Run workflow` на ветке патча либо после слияния в QA (не main). Альтернатива — создать и назначить одноразовую метку `aio-selfhosted-contracts-approved-once` авторскому PR, меняющему соответствующие файлы. Публикация в main запрещена без отдельной команды.
