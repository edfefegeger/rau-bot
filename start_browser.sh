#!/usr/bin/env bash
# НОВОЕ окно браузера с отладочным портом (отдельный профиль). Основной профиль: ./start_browser.sh --default-profile
cd "$(dirname "$0")" && python3 launcher.py browser "$@"
