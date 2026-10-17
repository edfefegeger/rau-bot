#!/usr/bin/env bash
# Запуск бота (config.json и targets.csv в этой папке). Перед этим: ./start_browser.sh и вход в кабинет.
cd "$(dirname "$0")" && python3 launcher.py run
