# ZINDAGI · платёжный бот «Премиум»

Пользователь жмёт «Оформить подписку» в приложении → попадает в этого бота → платит на карту → отправляет чек →
админ жмёт «Подтвердить» → бот пишет в Firebase `premium/<telegram-id>` → приложение разблокируется само.

## Установка (один раз)

**1. Новый бот.** В [@BotFather](https://t.me/BotFather): `/newbot` → сохраните токен и username.
Бот должен быть *отдельным* от `@zindagiRObot`: приложение само читает его сообщения (`getUpdates`) при регистрации,
и второй потребитель того же токена будет с ним конфликтовать.

**2. Ключ Firebase.** Firebase Console → ⚙ Project settings → Service accounts → *Generate new private key*.
Файл положите рядом с `bot.py` под именем `serviceAccount.json`. Никому не отправляйте и не публикуйте его.

**3. Правила базы.** Realtime Database → Rules. **Добавьте** узел `premium` внутрь `"rules"` (остальные правила не трогайте):

```json
"premium": {
  "$id": { ".read": true, ".write": false }
}
```
Читать статус может приложение, писать — только бот (он ходит по ключу из шага 2, правила ему не мешают).

**4. Настройки.** `cp .env.example .env` и заполните `BOT_TOKEN`, `ADMIN_IDS`. Админ должен открыть бота и нажать **Start** —
иначе бот не сможет присылать ему чеки.

**5. Приложение.** В `index.html` найдите строку `const PAY_BOT='';` и впишите username платёжного бота без @.

**6. Запуск.**
```bash
pip install -r requirements.txt
python bot.py
```
Бот должен работать постоянно (VPS, Railway, Render и т. п.). Пример для systemd (`/etc/systemd/system/zindagi-pay.service`):
```ini
[Service]
WorkingDirectory=/opt/zindagi-pay
ExecStart=/usr/bin/python3 bot.py
Restart=always
[Install]
WantedBy=multi-user.target
```
Заявки хранятся в `premium.sqlite3`: после перезапуска ожидающие чеки и сроки подписок не теряются.

## Команды админа
| Команда | Что делает |
|---|---|
| `/pending` | заново присылает все чеки, ожидающие решения |
| `/grant <id> [дней]` | выдать/продлить Премиум вручную (по умолчанию 30 дней) |
| `/revoke <id>` | отключить Премиум |

## Как ведёт себя подписка
- Оплата при уже активной подписке **продлевает** её: новый месяц добавляется к текущему сроку.
- За 3 дня до конца и в день окончания бот присылает напоминание с кнопкой «Продлить».
- Дополнительные чеки к заявке, которая уже на проверке, пересылаются админу (не более 3).

## Railway
Файлы `.env` и `serviceAccount.json` в репозиторий не кладём — всё задаётся в Railway.

1. New Project → Deploy from GitHub repo → выберите репозиторий (в настройках сервиса *Root Directory* = папка с `bot.py`).
   Start Command: `python bot.py`.
2. Вкладка **Variables** → добавьте:

| Переменная | Значение |
|---|---|
| `BOT_TOKEN` | токен платёжного бота |
| `ADMIN_IDS` | `8506743201` |
| `FIREBASE_DB_URL` | `https://zindagi-1c81c-default-rtdb.firebaseio.com` |
| `FIREBASE_CREDENTIALS_JSON` | **весь текст** скачанного файла ключа Firebase (от `{` до `}`) |
| `SQLITE_PATH` | `/data/premium.sqlite3` |

   Остальное (`CARD_NUMBER`, `PRICE_TJS` …) необязательно — по умолчанию стоят ваши значения.
3. **Volume** (Settings → Volumes → Add): mount path `/data`. Без него база заявок и сроков подписок стирается при каждом
   новом деплое.
4. Логи (Deployments → View logs) должны показать `Бот запущен: @PAYMENT_ZINDAGIBOT`.

`FIREBASE_CREDENTIALS_JSON` можно задать и в base64 (тогда вставьте base64-строку) — бот понимает оба варианта.
