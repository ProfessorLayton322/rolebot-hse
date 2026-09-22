from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import boto3

DEFAULT_KEY = "larp-bot/roleclub_achievement_basic.pptx"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download the read-only achievement PPTX build input")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--key", default=DEFAULT_KEY)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = arguments()
    client = boto3.client(
        "s3",
        endpoint_url="https://storage.yandexcloud.net",
        region_name="ru-central1",
    )
    response = client.get_object(Bucket=args.bucket, Key=args.key)
    content = response["Body"].read()
    try:
        with ZipFile(BytesIO(content)) as archive:
            slides = [
                name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            ]
            if slides != ["ppt/slides/slide1.xml"]:
                raise RuntimeError("achievement template must contain exactly one slide")
            if "ЛОГОС" not in archive.read("ppt/slides/slide1.xml").decode("utf-8"):
                raise RuntimeError("achievement template has no ЛОГОС placeholders")
    except BadZipFile as error:
        raise RuntimeError("achievement template is not a valid PPTX archive") from error
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(content)


if __name__ == "__main__":
    main()
