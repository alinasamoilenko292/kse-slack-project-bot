#!/bin/bash
# Оновити бота на сервері одною командою
# Запускати на сервері OVHcloud: bash deploy.sh

set -e

echo "⬇️  Тягну зміни з GitHub..."
git pull origin main

echo "🔄  Перезапускаю бота..."
sudo systemctl restart slackbot

echo "✅  Готово! Статус:"
sudo systemctl status slackbot --no-pager -l
