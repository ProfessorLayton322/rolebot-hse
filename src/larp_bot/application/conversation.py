"""Public conversation API assembled from focused workflow modules.

The re-export keeps existing imports stable while each workflow can be read,
tested, and changed without loading the complete state machine.
"""

from .conversation_admin_access import AdministrationAccessMixin
from .conversation_admin_creation import AdministrationCreationMixin
from .conversation_admin_dialog import AdministrationDialogMixin
from .conversation_admin_games import AdministrationGamesMixin
from .conversation_admin_roles import AdministrationRolesMixin
from .conversation_admin_rosters import AdministrationRostersMixin
from .conversation_core import ConversationCoreMixin
from .conversation_players import PlayerConversationMixin
from .conversation_profiles import ProfileConversationMixin
from .conversation_support import *  # noqa: F403
from .navigation import ADMIN_MENU as ADMIN_MENU
from .navigation import MAIN_MENU as MAIN_MENU


class ConversationEngine(
    ConversationCoreMixin,
    ProfileConversationMixin,
    PlayerConversationMixin,
    AdministrationAccessMixin,
    AdministrationCreationMixin,
    AdministrationDialogMixin,
    AdministrationRolesMixin,
    AdministrationRostersMixin,
    AdministrationGamesMixin,
):
    """One platform-neutral state machine rendered by Telegram and VK adapters."""
