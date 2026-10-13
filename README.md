# apisdkopti24-dashboard

Примеры использования SDK [apisdkopti24](https://pypi.org/project/apisdkopti24/)
для API топливных карт ОПТИ 24: дашборд с показателями и аналитикой, оповещения,
выгрузки.

## Прототип дашборда

Прототип опубликован на GitHub Pages:
[raspopovaa.github.io/apisdkopti24-dashboard](https://raspopovaa.github.io/apisdkopti24-dashboard/).

Это статическая страница: к API она не обращается. Четыре договора построены по ответам
DEMO API, синтетический договор «СН-000201» генерируется в браузере по обезличенному
профилю `prototype/synthetic-profile.json`; его реквизиты, баланс, платежи и карты условные.
Страница публикуется workflow `.github/workflows/pages.yml` при изменениях в `prototype/`.
Описание блоков и данных — в [docs/dashboard-spec.md](docs/dashboard-spec.md).

Запустить локально:

```bash
python scripts/serve_prototype.py
```

и открыть http://127.0.0.1:8765/.

Проект в разработке. Правила для ИИ-агентов — в [AGENTS.md](AGENTS.md).
