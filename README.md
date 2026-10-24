# ZINDAGI · платёжный бот «Премиум» — запуск на Bothost

## 1. Файлы в репозитории GitHub (в корне)
bot.py · requirements.txt · .gitignore  (`.env` и ключ Firebase в GitHub НЕ загружать!)

## 2. Ключ Firebase
Firebase Console → проект ZINDAGI → ⚙ Project settings → Service accounts →
Generate new private key. Откройте скачанный .json, скопируйте ВЕСЬ текст.

## 3. Создание бота на Bothost
1. Create a Bot → Python → подключить репозиторий GitHub (ветка main).
2. Главный файл: `bot.py`. Bothost сам ставит зависимости из requirements.txt.
3. В разделе переменных окружения добавьте:
   - BOT_TOKEN — токен из @BotFather
   - ADMIN_IDS — ваш Telegram ID
   - FIREBASE_CREDENTIALS_JSON — вставленный целиком JSON ключа
   - APP_URL — ссылка на приложение (по желанию)
   - DATA_DIR — постоянная папка для базы (по умолчанию /app/data; если в панели
     Bothost указан другой путь для постоянных данных — укажите его)
   Остальные (PRICE_TJS, CARD_NUMBER, CARD_HOLDER, PAY_NOTE, WAIT_MINUTES,
   PREMIUM_DAYS) — только если хотите изменить значения по умолчанию.
4. Запустите, в логах должно появиться: «Бот запущен: @имя_бота».

## 4. Обязательно
- Админ (из ADMIN_IDS) должен один раз нажать /start у этого бота, иначе чеки ему не придут.
- Платёжный бот и бот приложения — один и тот же бот, в который ведёт кнопка «Оформить подписку».
- Правила Firebase Realtime Database: запись `premium/<id>` делает бот через сервисный
  аккаунт (правила обходятся), приложению достаточно права чтения своей записи.

## 5. Команды
Пользователь: /start, /status, /help
Админ: /grant <id> [дней], /revoke <id>, /pending
