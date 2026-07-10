# Changelog

## 0.1.0 — 2026-07-10

- Первый рабочий релиз Telegram-бота для отслеживания цен Wildberries.
- Много товаров и несколько разрешённых Telegram-пользователей.
- Порог по проценту, сумме снижения или целевой цене.
- История изменений, уведомление о наличии, пауза, ручная проверка и удаление.
- Транзакционная очередь Telegram-уведомлений с pacing, retry и сохранением после перезапуска.
- Публичный пакетный provider с проверкой схемы и безопасным fallback через `curl`.
- Экспериментальная персональная цена через ручной вход в Chromium и зашифрованный
  Playwright storage state.
- Русский интерактивный установщик/меню, Docker hardening, backup/restore, диагностика,
  обновление с rollback и scoped uninstall.
- CI: Ruff, mypy, pytest, compileall, Bash syntax, Compose и Docker build.
