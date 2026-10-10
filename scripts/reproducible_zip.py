from __future__ import annotations

import sys
import zipfile
from pathlib import Path

source = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2]).resolve()
executables = set(sys.argv[3:])
files = sorted(path for path in source.rglob("*") if path.is_file())
with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in files:
        relative = path.relative_to(source).as_posix()
        info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        permissions = 0o755 if relative in executables else 0o644
        info.external_attr = (0o100000 | permissions) << 16
        archive.writestr(info, path.read_bytes())
