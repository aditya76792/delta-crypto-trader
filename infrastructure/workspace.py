"""
The ``trader/`` workspace: JSON files every desk tool reads and writes, so the
manual workflow chains like the reference skill pack:

    scanner → entry → exit → timemachine → budget → risk → broker (paper by default)
    alerts (while a position is open)          options (read-only chain analytics)

File contracts are documented in docs/CONTRACTS.md. Writes are atomic
(write temp + rename). Times are UTC ISO-8601; money is USD (Delta India settles
perpetuals in USD; INR is shown as an informational conversion only).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from infrastructure.config.settings import ROOT


def workspace_dir() -> Path:
    return Path(os.getenv("TRADER_WORKSPACE", ROOT / "trader"))


def path(name: str) -> Path:
    return workspace_dir() / name


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(name: str | Path, default: Any = None) -> Any:
    p = name if isinstance(name, Path) else path(name)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except ValueError as exc:
        raise ValueError(f"{p} is not valid JSON: {exc}") from exc


def write_json(name: str | Path, doc: Any) -> Path:
    p = name if isinstance(name, Path) else path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, default=str))
    tmp.replace(p)
    return p


def append_jsonl(name: str | Path, rec: dict[str, Any]) -> None:
    p = name if isinstance(name, Path) else path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def read_jsonl(name: str | Path) -> list[dict[str, Any]]:
    p = name if isinstance(name, Path) else path(name)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def fingerprint(order: dict[str, Any]) -> str:
    """Identity of an order (same fields → same fingerprint). Used to tie a risk
    verdict and the user's yes to EXACTLY the order that gets sent."""
    keys = ("symbol", "side", "size", "order_type", "limit_price", "leverage",
            "stop_loss", "take_profit", "reduce_only")
    ident = {k: order.get(k) for k in keys}
    return hashlib.sha1(json.dumps(ident, sort_keys=True, default=str).encode()).hexdigest()[:16]


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()
