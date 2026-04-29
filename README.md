# GeoGuessr Duels Analytics

Автоматически генерирует отчёт по дуэлям: статистика по странам, матрица путаницы, Move vs NoMove.

## Деплой

1. Создай репо на GitHub, залей содержимое
2. **Settings → Secrets and variables → Actions → New secret**
   - Name: `GEOGUESSR_NCFA`
   - Value: кука `_ncfa` с geoguessr.com (DevTools → Application → Cookies)
3. **Settings → Pages → Source: Deploy from branch → `main` / `/ (root)`**
4. **Actions → Update Duels Report → Run workflow** — первый запуск

Отчёт: `https://<user>.github.io/<repo>/`

## Обновление

- Автоматически каждый день в 5:00 UTC
- Или вручную: **Actions → Update Duels Report → Run workflow**

## Кука протухла?

`_ncfa` живёт несколько недель. Когда Action упадёт с 401 — обнови секрет:
**Settings → Secrets → GEOGUESSR_NCFA → Update**

## Локальный запуск

```bash
pip install -r requirements.txt
python fetch.py
# откроет duels_report.html
```
