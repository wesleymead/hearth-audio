#!/usr/bin/python3
"""Enable Hearth's narrowly scoped installed node helpers."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    if os.geteuid() != 0:
        print("Run this migration with sudo.", file=sys.stderr)
        return 1
    if len(sys.argv) != 2:
        print("usage: enable-node-audio-output.py /etc/hearth-audio/node.json", file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    stat = path.stat()
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Node configuration must contain a JSON object.")
    config["allowAudioOutputChanges"] = True
    config["allowNodePowerControl"] = True

    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(config, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, stat.st_mode)
        os.chown(temporary, stat.st_uid, stat.st_gid)
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
