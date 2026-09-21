# Agentic tool operating rules

- Use `gh`, `yc`, and pinned `wrangler` commands to inspect GitHub, Yandex Cloud, and Cloudflare when relevant.
- Push repository changes only to a non-default branch and deliver them through a pull request. Never push directly to `main`.
- Create exactly one pull request and start exactly one merge per user prompt. There is only one attempt: do not create corrective or follow-up pull requests for the same prompt.
- Use the repository's quiet `gh`-based PR script to create the pull request and start its merge. After the script starts the merge, end the prompt immediately without waiting for pull-request checks, merge completion, deployment, or smoke tests.
- Deploy or mutate Yandex Cloud application infrastructure only through the GitHub Actions Terraform CI/CD workflow. Direct `yc` deployment or resource-mutation commands are forbidden; read-only `yc` inspection is allowed.
- Always run the project's pinned `ruff format --check .` before delivery. If it fails, format the affected files with the pinned Ruff version and rerun the check until it passes.
- Preserve unrelated working-tree changes and verify behavior in proportion to deployment risk before considering work complete.
- In user requests and user-visible text, "gamemaster" and "master" mean the role stored internally as an event `leader`, while "moderator" means the role stored internally as `gamemaster`. Preserve the existing internal names in code, configuration, and YDB.
- Treat "user" and "global user" as synonyms: both mean the person behind mutually linked Telegram and VK accounts when both profiles point to each other. "Local user" is distinct and means only the concrete Telegram profile talking to the Telegram bot or VK profile talking to the VK bot.
