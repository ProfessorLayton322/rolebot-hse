import argparse
import importlib.util
import sys
from datetime import UTC, datetime
from statistics import median
from time import perf_counter

from larp_bot.adapters.yandex_disk.repository import showcase_workbook_bytes
from larp_bot.application.pass_preferences import event_pass_preferences
from larp_bot.domain.models import Platform, Registration, UserPassPreference
from larp_bot.domain.security import participant_key

parser = argparse.ArgumentParser(description="Compare admin XLSX CPU cost with a saved baseline source file.")
parser.add_argument("--baseline-source", required=True)
parser.add_argument("--repeats", type=int, default=11)
args = parser.parse_args()
spec = importlib.util.spec_from_file_location("baseline", args.baseline_source)
assert spec is not None and spec.loader is not None
baseline = importlib.util.module_from_spec(spec)
sys.modules["baseline"] = baseline
spec.loader.exec_module(baseline)
secret = "benchmark-secret"
event_id = "b49df060-85f7-45fd-851a-2e3dcf706320"
now = datetime.now(UTC)


def timed(fn):
    fn()
    values = []
    for _ in range(args.repeats):
        start = perf_counter()
        fn()
        values.append((perf_counter() - start) * 1000)
    return round(median(values), 2)


def benchmark(player_count, profile_count):
    rows = [
        Registration(
            event_id=event_id,
            participant_key=participant_key(secret, Platform.TELEGRAM, i, event_id),
            display_name=f"Фамилия Имя{i}",
            wish_play="Без пожеланий",
        )
        for i in range(player_count)
    ]
    raw = [
        dict(
            platform=Platform.TELEGRAM,
            user_id=i,
            needs_pass=bool(i % 2),
            telegram_handle=None,
            linked_vk_id=None,
            updated_at=now,
        )
        for i in range(profile_count)
    ]
    keys = {row.participant_key for row in rows}

    def projection():
        profiles = [UserPassPreference(**row) for row in raw]
        return event_pass_preferences(profiles, event_id=event_id, secret=secret, active_keys=keys)

    answers = projection()

    def current():
        return showcase_workbook_bytes(rows, needs_pass_by_key=projection())

    print(
        dict(
            players=player_count,
            profiles=profile_count,
            baseline_xlsx_ms=timed(lambda: baseline.showcase_workbook_bytes(rows)),
            new_xlsx_ms=timed(lambda: showcase_workbook_bytes(rows, needs_pass_by_key=answers)),
            decode_and_resolve_ms=timed(projection),
            new_total_cpu_ms=timed(current),
        ),
        flush=True,
    )


for player_count, profile_count in [(30, 1000), (30, 10000), (300, 10000), (30, 100000)]:
    benchmark(player_count, profile_count)
