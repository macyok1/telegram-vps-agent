# Установка собственного экземпляра

## 1. Подготовьте свои данные

Нужен отдельный Linux VPS с systemd и Python 3.12+ (инструкция рассчитана на Ubuntu
24.04). Для LiteLLM с PostgreSQL планируйте примерно 4 GB RAM и достаточно места
для Docker-образов и базы. Это ориентир, а не измеренный минимум.

1. В официальном `@BotFather` создайте собственного бота через `/newbot`.
2. Узнайте числовой ID **своего пользователя Telegram**, не username и не ID бота.
   Нейросеть может прочитать `message.from.id` из вашего личного `/start` через
   `getUpdates` до запуска сервиса. Не публикуйте URL с токеном или JSON переписки.
3. Подготовьте собственный API-ключ LLM-провайдера, его API base и ID модели с
   поддержкой tool calling. Если уже есть LiteLLM, подготовьте URL и его ключи.

Бот работает через long polling. Внешний HTTP-порт и домен ему не нужны.
На одном Telegram-токене должен работать один polling-процесс; существующий webhook
нужно отключить. [Telegram Bot API](https://core.telegram.org/bots/api#getupdates).

## 2. Скачайте код на новый VPS

```bash
sudo apt-get update
sudo apt-get install -y python3 git curl
git clone https://github.com/macyok1/telegram-vps-agent.git
cd telegram-vps-agent
sudo bash scripts/install.sh
```

Установщик создаёт каталоги, пустой vault, закрытые env, системного пользователя
`admin-panel` для совместимости сокетов и два unit-файла. Сервисы не запускаются.
При обнаружении существующих путей установки или пользователя он останавливается
до записи, чтобы не заменить уже работающий экземпляр.

## 3. Подключите LiteLLM

Если LiteLLM уже есть, пропустите создание нового gateway. Проверьте доступность
его URL с VPS и наличие выбранного маршрута в `/v1/models`. У URL бота не должно
быть завершающего `/v1`: бот добавляет API-путь сам.

Для нового gateway сначала установите Docker Engine и Compose plugin по
[официальной инструкции Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
Затем:

```bash
sudo install -d -m 0700 /opt/ai-gateway
sudo install -m 0644 gateway/compose.yml /opt/ai-gateway/compose.yml
sudo install -m 0644 gateway/config.yaml /opt/ai-gateway/config.yaml
sudo install -m 0600 gateway/gateway.env.example /opt/ai-gateway/.env
sudo nano /opt/ai-gateway/.env
sudo nano /opt/ai-gateway/config.yaml
```

В `.env` замените все `REPLACE_...` на собственные значения. Для master/salt/UI/DB
нужны разные случайные значения: например, каждое сгенерируйте отдельно командой
`python3 -c 'import secrets; print(secrets.token_hex(32))'` в собственной закрытой
консоли. Для master key можно добавить префикс `sk-`. Hex-пароль PostgreSQL не
требует URL-экранирования. Сохраните salt key: смена его значения мешает чтению
ранее зашифрованных в БД ключей. Названия и назначение параметров описаны в
[официальном quickstart LiteLLM](https://docs.litellm.ai/docs/proxy/docker_quick_start).

В `config.yaml` оставьте маршрут `model_name: bot-model`, замените
`openai/REPLACE_WITH_YOUR_MODEL_ID` на реальную модель своего OpenAI-compatible API.
Укажите правильный `UPSTREAM_API_BASE` в `.env`. Для native-провайдера адаптируйте
`litellm_params` по [документации LiteLLM](https://docs.litellm.ai/docs/proxy/configs).

```bash
sudo docker compose --project-directory /opt/ai-gateway -f /opt/ai-gateway/compose.yml config -q
sudo docker compose --project-directory /opt/ai-gateway -f /opt/ai-gateway/compose.yml up -d
curl --fail http://127.0.0.1:4000/health/readiness
```

Gateway слушает только `127.0.0.1:4000`, PostgreSQL не публикует порт. Для UI можно
создать SSH-туннель с вашего компьютера:

```bash
ssh -N -L 4000:127.0.0.1:4000 YOUR_USER@YOUR_VPS
```

Откройте `http://127.0.0.1:4000/ui` и войдите своим `UI_USERNAME`/`UI_PASSWORD`.
Создайте virtual key для чата с доступом к `bot-model` и нужными лимитами.
Он будет `LITELLM_CHAT_KEY`; master key используется для управления gateway.

## 4. Заполните конфигурацию бота

```bash
sudo nano /etc/litellm-telegram-bot.env
```

Обязательны:

| Переменная | Ваше значение |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Токен вашего нового бота |
| `TELEGRAM_ADMIN_ID` | Числовой Telegram ID владельца |
| `LITELLM_INTERNAL_URL` | URL LiteLLM без `/v1` |
| `LITELLM_CHAT_KEY` | Chat virtual key вашего gateway |
| `LITELLM_MASTER_KEY` | Master key вашего gateway |
| `LITELLM_BOT_MODEL` | Реальный маршрут, например `bot-model` |

Формат env: `NAME=value`, без `export`. Для совместимости systemd и брокера
используйте одно значение на строку, без shell-подстановок и комментариев после значения.
Оставьте `ALLOWED_USERS`/`ALLOWED_GROUPS` пустыми, Supermemory выключенной,
fallback-переменную пустой, пока не настроили реальные резервные маршруты.

Установщик уже создал пустой `/var/lib/litellm-telegram-bot/vault.json` с правами 0600.
Файл `/etc/admin-panel.env` нужен брокеру и содержит независимо сгенерированный
пароль для потенциальной будущей веб-консоли. Веб-панель здесь не устанавливается.
Не подставляйте в эти файлы данные или ID автора репозитория.

## 5. Проверка и запуск

```bash
sudo python3 scripts/check-config.py
sudo systemctl enable --now admin-console litellm-telegram-bot
sudo systemctl is-active admin-console litellm-telegram-bot
sudo journalctl -u litellm-telegram-bot -n 50 --no-pager
```

`check-config.py` проверяет файлы, Telegram, gateway, наличие модели и делает один
короткий оплачиваемый запрос модели без vault и shell-инструментов. Успешный результат
не заменяет проверку tool calling: она выполняется следующим шагом.

В личном чате вашего нового бота проверьте:

1. `/start`, `/status`, `/models`, `/use bot-model`.
2. Обычный вопрос, чтобы получить ответ вашей модели.
3. «Выполни `whoami` и `date -Is`, покажи фактический вывод». Ожидается root и
   реальная дата; если инструмент не вызван, проверьте поддержку tools у модели.
4. `/vault set demo EXAMPLE_ONLY` → «Сохранить сейчас» → `/vault show demo` →
   `/vault del demo`. Не используйте настоящий пароль для первой проверки.

Если команды недоступны, проверьте `admin-console`, его журнал и
`/run/admin-console/control.sock`. Одного работающего LiteLLM недостаточно.
Перезапуск бота сбрасывает его краткую Telegram-историю и выбор модели к env;
vault, очередь автосохранения и локальные заметки сохраняются на диске.

## 6. Обслуживание

Сохраняйте закрытые резервные копии `/etc/litellm-telegram-bot.env`,
`/etc/admin-panel.env`, каталогов `/var/lib/litellm-telegram-bot`,
`/var/lib/admin-console` и gateway env/БД. Они могут содержать секреты и вывод команд.
Не коммитьте их и не пересылайте вместе с исходниками.

Для обновления сначала сделайте копии, проведите тесты новой версии, затем замените
только Python-файлы приложений и перезапустите сервисы. Установщик не предназначен
для обновления существующего экземпляра и не должен обходиться удалением state/env.
Не меняйте SSH/firewall и не переносите настройки чужого VPS как часть установки бота.
