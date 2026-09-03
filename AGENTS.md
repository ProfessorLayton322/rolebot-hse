# Agentic tool operating rules

- Use `gh`, `yc`, and pinned `wrangler` commands to inspect GitHub, Yandex Cloud, and Cloudflare when relevant.
- Push repository changes only to a non-default branch and deliver them through a pull request. Never push directly to `main`.
- Create exactly one pull request and start exactly one merge per user prompt. There is only one attempt: do not create corrective or follow-up pull requests for the same prompt.
- Use the repository's quiet `gh`-based PR script to create the pull request and start its merge. After the script starts the merge, end the prompt immediately without waiting for pull-request checks, merge completion, deployment, or smoke tests.
- Deploy or mutate Yandex Cloud application infrastructure only through the GitHub Actions Terraform CI/CD workflow. Direct `yc` deployment or resource-mutation commands are forbidden; read-only `yc` inspection is allowed.
- Preserve unrelated working-tree changes and verify behavior in proportion to deployment risk before considering work complete.
