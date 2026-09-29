"""Keep scheduling callbacks within Telegram's 64-byte limit with UUID event IDs."""


def encode_callback(value: str) -> str:
    if value.startswith("ag:schedule-"):
        return "ag:sc-" + value.removeprefix("ag:schedule-")
    return value


def decode_callback(value: str) -> str:
    if value.startswith("ag:sc-"):
        return "ag:schedule-" + value.removeprefix("ag:sc-")
    # Previously sent keyboards and canonical application callbacks remain valid.
    return value
