# Cloud and database direction

- Yandex Cloud and YDB are frozen legacy targets. Commit `01aab7f35014c2b18b2c7ff489c3b3cbe76722f3` is the final Yandex-supporting baseline; preserve it in Git history as the reference for the last known-good Yandex state.
- Do not develop, repair, deploy, migrate, or maintain feature parity for Yandex Cloud or YDB. Preserve the existing legacy implementation unless the user explicitly requests its removal.
- From the commit after that baseline onward, all cloud infrastructure and database development must target AWS and DynamoDB only.
- If the user explicitly requests a future return to Yandex Cloud or YDB, derive the complete catch-up work from the preserved baseline and the then-current AWS/DynamoDB implementation, then implement the required migration as a new effort. Do not proactively keep Yandex ready in the meantime.
- Use `yc` only for read-only inspection of the frozen legacy state or for planning an explicitly requested return to Yandex.

# Repository terminology

- Treat "user" and "global user" as synonyms: both mean the person behind mutually linked Telegram and VK accounts when both profiles point to each other.
- "Local user" is distinct and means only the concrete Telegram profile talking to the Telegram bot or VK profile talking to the VK bot.
- Keep the Russian PDF administrator guide and its source ordered so the first substantive section is the quick end-to-end game lifecycle, followed immediately by Telegram chat setup and management. In that quick guide, keep chat creation, the bot's required administrator rights, and `/tie` in one step, followed by a separate step for sending the stored link to confirmed players.
