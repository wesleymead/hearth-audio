#!/usr/bin/env python3
"""Install display units for the actual Raspberry Pi OS login user."""

from __future__ import annotations

import argparse
import grp
import pwd
import re
from pathlib import Path


def render(template: str, user: str, group: str, home: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise ValueError("Unsafe display user")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", group):
        raise ValueError("Unsafe display group")
    supplemental = [name for name in ("hearth", "video", "render", "audio", "input", "dialout", "i2c") if _group_exists(name)]
    return (template.replace("User=pi\n", f"User={user}\n")
            .replace("Group=pi\n", f"Group={group}\n")
            .replace("Environment=HOME=/home/pi\n", f"Environment=HOME={home}\n")
            .replace("SupplementaryGroups=hearth video render audio input dialout i2c\n", f"SupplementaryGroups={' '.join(supplemental)}\n"))


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", default="/etc/systemd/system")
    args = parser.parse_args()
    account = pwd.getpwnam(args.user)
    group = grp.getgrgid(account.pw_gid).gr_name
    source = Path(args.source)
    destination = Path(args.destination)
    for name in ("hearth-node-display.service", "hearth-node-hdmi-display.service"):
        data = render((source / name).read_text(encoding="utf-8"), args.user, group, account.pw_dir)
        (destination / name).write_text(data, encoding="utf-8")
        (destination / name).chmod(0o644)
    print(f"Display services will run as {args.user}")


if __name__ == "__main__":
    main()
