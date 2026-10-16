"""Small, dependency-free parser for Flipper Zero infrared files."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


SUPPORTED_PARSED_PROTOCOLS = {
    "NEC",
    "NECext",
    "NEC42",
    "NEC42ext",
    "Samsung32",
    "RC6",
    "RC5",
    "RC5X",
    "SIRC",
    "SIRC15",
    "SIRC20",
    "Kaseikyo",
    "RCA",
}


@dataclass(frozen=True)
class InfraredSignal:
    name: str
    kind: str
    protocol: str | None = None
    address: str | None = None
    command: str | None = None
    frequency: int | None = None
    duty_cycle: float | None = None
    data: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["data"] = list(self.data)
        return value


class InfraredFileError(ValueError):
    """Raised when a Flipper infrared file is malformed."""


def _parse_block(lines: list[str], block_number: int) -> InfraredSignal:
    values: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            raise InfraredFileError(f"Block {block_number}: expected 'key: value'.")
        key, value = line.split(":", 1)
        values[key.strip().lower()] = value.strip()

    name = values.get("name", "").strip()
    kind = values.get("type", "").strip().lower()
    if not name:
        raise InfraredFileError(f"Block {block_number}: missing signal name.")
    if kind not in {"parsed", "raw"}:
        raise InfraredFileError(f"Block {block_number}: type must be parsed or raw.")

    if kind == "parsed":
        protocol = values.get("protocol", "")
        address = values.get("address", "")
        command = values.get("command", "")
        if not protocol or not address or not command:
            raise InfraredFileError(
                f"Block {block_number}: parsed signals need protocol, address, and command."
            )
        return InfraredSignal(
            name=name,
            kind=kind,
            protocol=protocol,
            address=address,
            command=command,
        )

    try:
        frequency = int(values.get("frequency", "0"))
        duty_cycle = float(values.get("duty_cycle", "0"))
        data = tuple(int(item) for item in values.get("data", "").split())
    except ValueError as exc:
        raise InfraredFileError(f"Block {block_number}: invalid raw signal values.") from exc
    if not 20_000 <= frequency <= 1_000_000:
        raise InfraredFileError(f"Block {block_number}: unreasonable carrier frequency.")
    if not 0 < duty_cycle <= 1:
        raise InfraredFileError(f"Block {block_number}: duty cycle must be between 0 and 1.")
    if not data or len(data) > 1024 or any(duration <= 0 for duration in data):
        raise InfraredFileError(
            f"Block {block_number}: raw data needs 1-1024 positive pulse durations."
        )
    return InfraredSignal(
        name=name,
        kind=kind,
        frequency=frequency,
        duty_cycle=duty_cycle,
        data=data,
    )


def parse_flipper_ir(content: str) -> list[InfraredSignal]:
    """Parse a Version 1 Flipper ``.ir`` remote file."""

    if not isinstance(content, str) or not content.strip():
        raise InfraredFileError("The infrared file is empty.")

    header: dict[str, str] = {}
    blocks: list[list[str]] = []
    current: list[str] = []
    in_signals = False

    for raw_line in content.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line == "#":
            if current:
                blocks.append(current)
                current = []
            in_signals = True
            continue
        if line.startswith("#"):
            continue
        if not in_signals:
            if ":" not in line:
                raise InfraredFileError("Malformed file header.")
            key, value = line.split(":", 1)
            header[key.strip().lower()] = value.strip()
        else:
            current.append(line)

    if current:
        blocks.append(current)

    if header.get("filetype") not in {"IR signals file", "IR library file"}:
        raise InfraredFileError("Unsupported Flipper file type.")
    if header.get("version") != "1":
        raise InfraredFileError("Only Flipper infrared format version 1 is supported.")
    if not blocks:
        raise InfraredFileError("No infrared signals were found.")

    return [_parse_block(block, index) for index, block in enumerate(blocks, start=1)]
