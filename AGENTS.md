# Правила обновления бота

## Уведомления пользователей

- После каждого успешно развёрнутого обновления бота отправлять всем зарегистрированным пользователям одно краткое уведомление на русском через `mcc-broadcast`.
- Перед рассылкой проверить, что новая версия запущена и бот доступен. Не объявлять об обновлении, если деплой не завершился успешно.
- В 1–3 коротких пунктах описывать только пользовательские изменения: функции, исправления, данные и лимиты. Не включать внутренние технические подробности.
- Писать простым языком, понятным человеку без технических знаний: что изменилось для пользователя и что теперь можно сделать в боте. Не использовать слова вроде «коммит», «деплой», «API» и другие термины разработки.
- Если изменились команды или меню и пользователям может понадобиться обновить его, добавить: «Если меню не обновилось, вызовите /start». Если это не требуется, не просить вызывать `/start`.
- Не отправлять уведомление повторно при обычном перезапуске сервиса или повторном выполнении того же деплоя. При частичной доставке не повторять рассылку всем подряд.
- Проверять итоговые счётчики рассылки и сообщать владельцу бота о недоставленных уведомлениях. Не выводить токен и идентификаторы пользователей.
- `mcc-broadcast` всегда отправляет сообщения без звука (`disable_notification=True`). Не добавлять режим громкой рассылки и не переопределять это поведение для отдельных запусков.

# Shared Telegram Bot Rules

## Production release notifications

- A production deployment with user-facing changes is complete only after all eligible bot users receive one concise release notification in Russian, unless the user explicitly suppresses or postpones it.
- Treat an explicit request to deploy a bot as authorization to send that one release notification through the repository's existing broadcast mechanism. Repository-specific rules define the audience, command, version field, and runtime evidence.
- Before sending, verify that the new version is running and the bot is available. Do not announce local-only work, an unsuccessful or rolled-back deployment, tests, documentation-only changes, or agent-only files.
- Describe only deployed user-visible behavior in one to three short points. Use plain language and omit internal terms such as commit, deployment, API, database, or migration.
- Mention `/start` only when commands or menus changed and users may need it to refresh the interface.
- Send release notifications silently when the existing mechanism supports it. A repository-specific rule may make silent delivery mandatory.
- Make delivery idempotent for the release or version. Do not resend to everyone after an ordinary restart or repeated deployment, and retry only recipients whose earlier delivery failed.
- Verify final sent and failed counts and report only those counts to the owner. Never expose bot tokens, chat IDs, usernames, or other user data.
- Preserve any explicit user instruction to suppress, postpone, or customize the release notification.
- Keep project-specific business rules and delivery mechanics in the repository's own `AGENTS.md`; they may specialize this contract without weakening its delivery and privacy guarantees.

## Telegram Bot Button Rules

1. **Keyboard buttons = main bot navigation.**
   Use them for the main sections and the most frequently used actions.
2. **Keep the keyboard consistent.**
   Do not change it when the user moves between sections or screens.
3. **Change the keyboard only when the interaction mode changes.**
   For example, during a separate step-by-step flow. Restore the main keyboard when the flow is finished.
4. **Buttons under a message = actions for the current screen.**
   Use them for anything related to the current section, object, or message.
5. **Put the “Back” button under the message.**
   Its destination depends on the current screen.
6. **Show lists of objects under the message.**
   For example: recipes, groups, cards, banks, products, etc.
7. **Keep the keyboard short and stable.**
   Do not put every bot function there. Keep only the main entry points.
8. **Do not duplicate the same navigation in both the keyboard and under the message unless necessary.**
9. **For navigation inside one section, update the current message when possible** instead of sending a new menu message after every click.
10. **Main rule:**
    **Keyboard = where to go.**
    **Under the message = what to do here.**

> **Change the keyboard because the interaction mode changed, not because the screen changed.**

## Form flows

- In a separate form or editor flow, place cancellation only in the reply keyboard and restore the main keyboard when the flow ends.
- Keep one current form message. Edit it when possible; before sending a replacement, remove the previous bound message. If removal fails, do not create a duplicate form.

## Repository-specific Telegram behavior

- When creating a store after an unsuccessful search, prefill the entered store name.
