#!/usr/bin/env bash
# Подготовка нового экземпляра. Не запускает бота до заполнения конфигурации.
set -euo pipefail
if [[ "${EUID}" -ne 0 ]]; then
  echo 'Запустите: sudo bash scripts/install.sh' >&2
  exit 1
fi
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
command -v python3 >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Нужен Python 3.12+"'
command -v systemctl >/dev/null
# Установка рассчитана на отдельный новый VPS; существующий экземпляр не заменяется.
for target in /opt/litellm-telegram-bot /opt/admin-console \
  /etc/litellm-telegram-bot.env /etc/admin-panel.env \
  /var/lib/litellm-telegram-bot /var/lib/admin-console \
  /etc/systemd/system/litellm-telegram-bot.service \
  /etc/systemd/system/admin-console.service; do
  if [[ -e "$target" || -L "$target" ]]; then
    echo "Уже существует $target. Установка остановлена, данные не заменены." >&2
    exit 1
  fi
done
if getent passwd admin-panel >/dev/null || getent group admin-panel >/dev/null; then
  echo 'Пользователь или группа admin-panel уже существуют. Используйте отдельный VPS.' >&2
  exit 1
fi
umask 077
useradd --system --user-group --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin admin-panel
install -d -m 0755 /opt/litellm-telegram-bot /opt/admin-console
install -d -m 0700 /var/lib/litellm-telegram-bot /var/lib/admin-console
for source in "$repo_root"/apps/telegram-bot/*.py; do
  [[ "$(basename -- "$source")" == test_* ]] && continue
  install -m 0644 "$source" /opt/litellm-telegram-bot/
done
for source in "$repo_root"/apps/*.py; do
  install -m 0644 "$source" /opt/litellm-telegram-bot/
done
for name in server_console console_jobs command_policy tool_approval; do
  install -m 0644 "$repo_root/apps/$name.py" /opt/admin-console/
done
install -m 0600 "$repo_root/config/bot.env.example" /etc/litellm-telegram-bot.env
python3 - <<'PY'
from pathlib import Path
import secrets
# Совместимость с брокером; веб-панель этим установщиком не разворачивается.
Path('/etc/admin-panel.env').write_text(
    'CODEX_ADMIN_PASSWORD=' + secrets.token_hex(32) + '\n', encoding='utf-8')
Path('/var/lib/litellm-telegram-bot/vault.json').write_text('{}\n', encoding='utf-8')
PY
chmod 0600 /etc/admin-panel.env /var/lib/litellm-telegram-bot/vault.json
install -m 0644 "$repo_root/config/admin-console.service" /etc/systemd/system/
install -m 0644 "$repo_root/config/litellm-telegram-bot.service" /etc/systemd/system/
systemctl daemon-reload
echo 'Файлы установлены. Сервисы ещё не запущены.'
echo 'Заполните /etc/litellm-telegram-bot.env своими доступами.'
echo 'Настройте LiteLLM, затем: sudo python3 scripts/check-config.py'
echo 'После успешной проверки: sudo systemctl enable --now admin-console litellm-telegram-bot'
