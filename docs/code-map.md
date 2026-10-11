# Code map

Use this map to open only the capability involved in a change. The compatibility
modules `application/conversation.py`, `application/services.py`, and
`adapters/ydb/repositories.py` preserve existing imports; they should remain
small and contain no business logic.

## Conversation workflows

| Change | Module |
|---|---|
| Request entry, persistence, top-level dispatch | `conversation_core.py` |
| Profile creation and editing | `conversation_profiles.py` |
| Signup, confirmation, wishes, cancellation | `conversation_players.py` |
| Admin authorization, menus, passes, master lookup | `conversation_admin_access.py` |
| Game creation and initial master assignment | `conversation_admin_creation.py` |
| Admin callback and free-text state routing | `conversation_admin_dialog.py` |
| Moderator and event-master mutations | `conversation_admin_roles.py` |
| Rosters, player profiles, reserve promotion, statistics menus | `conversation_admin_rosters.py` |
| Event lifecycle, scheduling, files, and notifications | `conversation_admin_games.py` |
| Shared labels, prompts, buttons, and pure render helpers | `conversation_support.py` |
| Cross-workflow typing contract | `conversation_context.py` |

The concrete `ConversationEngine` composes the workflow mixins. A workflow may
call another workflow through the typed context, but reusable policy belongs in
a service or a pure support function rather than in the façade.

## Application services

The service dependency direction is:

`service_support / notification_helpers → registration_services → administration_services / notification_services → ordered_mutations`

| Change | Module |
|---|---|
| Domain errors, mutation results, tokens, paths, time-conflict policy | `service_support.py` |
| Pure chat-link and active-roster policy | `notification_helpers.py` |
| Registration catalog, pages, and command publishing | `registration_services.py` |
| Event administration and table exports | `administration_services.py` |
| Participant/master notification delivery | `notification_services.py` |
| Serialized event and registration mutations | `ordered_mutations.py` |

Keep dependencies flowing in that direction to avoid circular imports. Put a
new service in the narrowest module that owns its invariant.

## YDB persistence

| Change | Module |
|---|---|
| SDK executor and row/value conversion | `adapters/ydb/common.py` |
| Telegram/VK user persistence | `adapters/ydb/user_repository.py` |
| Event lifecycle persistence | `adapters/ydb/event_repository.py` |
| Registration persistence | `adapters/ydb/registration_repository.py` |

Tests that replace clocks, deadline helpers, or SDK constructors should patch
the owning module above. Public application imports should continue to use the
compatibility modules.
