from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from larp_bot.adapters.google_drive.tables import GoogleDriveMirroredUploader
from larp_bot.adapters.memory import MemoryEventRepository, MemoryRegistrationRepository, MemoryUserRepository
from larp_bot.adapters.yandex_disk.repository import YandexDiskShowcaseRepository
from larp_bot.application.services import EventAdministrationService, RegistrationCatalog
from larp_bot.domain.models import BotIdentity, Event
from tests.conftest import MemoryDiskStore
from tests.unit.test_google_drive_tables import MemoryTableDrive, RecordingUploader


class CompletionWinsEventRepository(MemoryEventRepository):
    async def create(self, event: Event, leaders: Sequence[BotIdentity] = ()) -> None:
        await super().create(
            Event.model_validate(
                event.model_dump()
                | {
                    "public_registration_url": "https://disk.example/master",
                    "public_table_public_url": "https://disk.example/public",
                    "pass_table_public_url": "https://disk.example/pass",
                }
            ),
            leaders,
        )
        raise ValueError("event exists")


async def test_system_refresh_backfills_admin_player_and_pass_tables_to_google() -> None:
    event = Event(
        event_id="event-legacy",
        name="Legacy game",
        disk_resource_path="disk:/larp-bot/events/legacy-master.xlsx",
        public_registration_url="https://disk.example/legacy-master",
        registrations_migrated_at=None,
    )
    events = MemoryEventRepository([event])
    registrations = MemoryRegistrationRepository()
    users = MemoryUserRepository()
    drive = MemoryTableDrive()
    uploader = GoogleDriveMirroredUploader(RecordingUploader(), drive, "tables-folder")
    showcase = YandexDiskShowcaseRepository(MemoryDiskStore(), uploader)
    catalog = RegistrationCatalog(
        events,
        registrations,
        showcase,
        users=users,
        participant_secret="participant-secret",
        legacy_registration_imports=False,
    )
    administration = EventAdministrationService(events, showcase, catalog, users, "participant-secret")

    refreshed = await administration.refresh_event_tables(event.event_id)

    assert refreshed.master_table_google_url is not None
    assert refreshed.public_table_google_url is not None
    assert refreshed.pass_table_google_url is not None
    assert refreshed.registrations_migrated_at is not None
    assert refreshed.public_table_public_url == refreshed.public_table_google_url
    assert refreshed.pass_table_public_url == refreshed.pass_table_google_url
    assert {
        "events — legacy-master.xlsx",
        "events — event-legacy — public_table_Legacy game.xlsx",
        "passes — event-legacy.xlsx",
    } <= {name for _, name in drive.contents}


async def test_new_event_persists_all_google_showcase_urls_immediately() -> None:
    events = MemoryEventRepository()
    registrations = MemoryRegistrationRepository()
    users = MemoryUserRepository()
    drive = MemoryTableDrive()
    uploader = GoogleDriveMirroredUploader(RecordingUploader(), drive, "tables-folder")
    showcase = YandexDiskShowcaseRepository(MemoryDiskStore(), uploader)
    catalog = RegistrationCatalog(
        events,
        registrations,
        showcase,
        users=users,
        participant_secret="participant-secret",
    )
    administration = EventAdministrationService(events, showcase, catalog, users, "participant-secret")

    created = await administration.create_event("Google game", date(2026, 11, 14), 20, [])

    assert created is not None
    assert created.master_table_google_url is not None
    assert created.public_table_google_url is not None
    assert created.pass_table_google_url is not None
    assert created.public_registration_url == created.master_table_google_url
    assert created.public_table_public_url == created.public_table_google_url
    assert created.pass_table_public_url == created.pass_table_google_url


async def test_new_event_accepts_a_faster_yandex_enrichment_completion() -> None:
    events = CompletionWinsEventRepository()
    registrations = MemoryRegistrationRepository()
    users = MemoryUserRepository()
    uploader = GoogleDriveMirroredUploader(RecordingUploader(), MemoryTableDrive(), "tables-folder")
    showcase = YandexDiskShowcaseRepository(MemoryDiskStore(), uploader)
    catalog = RegistrationCatalog(
        events,
        registrations,
        showcase,
        users=users,
        participant_secret="participant-secret",
    )
    administration = EventAdministrationService(events, showcase, catalog, users, "participant-secret")

    created = await administration.create_event("Racing game", date(2026, 11, 14), 20, [])

    assert created is not None
    assert created.public_registration_url == "https://disk.example/master"
    assert created.public_table_public_url == "https://disk.example/public"
    assert created.pass_table_public_url == "https://disk.example/pass"
    assert created.master_table_google_url is not None
    assert created.public_table_google_url is not None
    assert created.pass_table_google_url is not None
