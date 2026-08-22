# Agentic tool operating rules

- Use `gh`, `yc`, and pinned `wrangler` commands to inspect GitHub, Yandex Cloud, and Cloudflare when relevant.
- Push repository changes only to a non-default branch and deliver them through a pull request. Never push directly to `main`.
- Keep iterating until the requested outcome is actually working. Monitor pull-request checks and post-merge deployment/smoke-test runs; when an attempted deployment fails, diagnose it and use a separate corrective pull request when needed.
- Deploy or mutate Yandex Cloud application infrastructure only through the GitHub Actions Terraform CI/CD workflow. Direct `yc` deployment or resource-mutation commands are forbidden; read-only `yc` inspection is allowed.
- Preserve unrelated working-tree changes and verify behavior in proportion to deployment risk before considering work complete.
