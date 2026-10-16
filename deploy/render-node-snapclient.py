#!/usr/bin/env python3
"""Set Snapclient's server from this node's configured Hub URL."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/hearth-audio/node.json")
    parser.add_argument("--output", default="/etc/default/snapclient")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    url = urlparse(config["hubUrl"])
    host = url.hostname
    if url.scheme not in {"http", "https"} or not host or not all(c.isalnum() or c in ".-:" for c in host):
        raise ValueError("Invalid Hub hostname in node configuration")
    contents = 'START_SNAPCLIENT=true\nSNAPCLIENT_OPTS="--host ' + host + ' --player alsa --mixer software --soundcard default"\n'
    Path(args.output).write_text(contents, encoding="utf-8")


if __name__ == "__main__":
    main()
