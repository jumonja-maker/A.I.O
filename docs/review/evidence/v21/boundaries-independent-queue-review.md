# Независимое review queue/save — boundary reviewer

Проверены изменения `core.py`, `result_store.py`, `save_verification.py` и релевантные HTTP-вызовы.

- Новый `outputCount`: старые записи без поля остаются читаемыми; повреждённая новая транзакция не получает статус успеха, доступные файлы уходят в salvage; error с нулём outputs остаётся валидным. Выпавший файл/пустой legacy-success устанавливает recovery_error, поэтому «все сохранившиеся saved» не удаляет исходники.
- NFC: canonical используется для проверки безопасных/дублирующих имён, raw path — для реального открытия. Это соответствует NTFS/Linux и сохраняет защиту от относительных путей/коллизий.
- Перенос `_drop_source` за глобальный lock сопровождается I/O lease. Метаинформация removed уже исключена; параллельный preflight работает с оставшимися каталогами, а release не удаляет весь root до освобождения lease. `_unreserve` остаётся под lock.
- `_get` после close возвращает структурированный GuardError/503, пойманный API. Старые reader leases продолжают читать владевшийся файл без нового `_get`; последний cleanup освобождает их.
- В новой логике shutdown проверены `job.running/io/pending_journal/cleaning`, journal write до `running=False`, сохранение OS lock до выхода зависшего reader. Нового deadlock/uncaught API exception не установлено.

## Дополнительный подтверждённый дефект (передан queue-agent и root)

В delete-ветке `_cleanup` по-прежнему (это существовало до v21) вызывается `job.store.close()` перед `shutil.rmtree(job.root)`. На медленной ФС второй AIO может захватить уже освобождённый `owner.lock` и восстановить каталог, который первый сейчас удалит.

Воспроизведение на Linux с двумя реальными ConverterGuard/ResultStore и барьером только перед rmtree:
1. First готовит unsaved PDF с retain_sources.
2. First.release(discard=True) доходит до rmtree; OS lock уже закрыт.
3. Second.recoverable() успешно принимает тот же jobId.
4. Rmtree First продолжается: root перестаёт существовать.
5. Second.open_result(своего восстановленного ticket) возвращает SAVE_CHANGED.

Это нарушение межпроцессного владения, не умозрительное условие. Требуется retirement/tombstone протокол либо другое атомарное исключение восстановления, совместимое с Windows; один глобальный Python lock проблему не решит. Изменения чужих файлов reviewer не делал.

## Повторное review после исправления retirement

Queue-agent добавил parent-side checksummed `.root.retiring.json`, связанный с rootName/device/inode. Маркер записывается под старым owner lock, а recovery проверяет его после получения блокировки, до чтения header или orphan salvage. Он находится вне удаляемого дерева и переживает любой порядок удаления owner.lock/job.json. Для Windows дескриптор owner закрывается перед rmtree; к этому моменту каталог уже исключён из admission.

Независимо повторено исходное межпроцессное воспроизведение: marker присутствует; второй экземпляр возвращает **0 восстановленных jobs** и `RECOVERY_CLEANUP_RETRY`; первый завершает явно запрошенное удаление, root и marker исчезают. Дополнительно **19/19** queue/save тестов PASS в отдельном прогоне (`boundary-review-retirement-tests.log`). Изучены failed marker commit, partial rmtree, invalid/foreign identity, stale finished marker и сохранение несохранённых данных. Блокеров не найдено.

Оговорки протокола: два совместимых v21 экземпляра, process-crash/обычные ошибки ФС. Это не доказательство power-loss durability или совместной работы со старым Guard, который не понимает retirement marker. Предположение о RecursionError на глубоко вложенном <8KiB JSON не воспроизвелось на локальном Python 3.12 и **не считается найденным дефектом**; автор добавил лишь защитное приведение исключения для других runtime.
