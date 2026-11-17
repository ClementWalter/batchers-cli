#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click==8.3.3",
#     "requests==2.32.5",
#     "eth-abi==5.2.0",
#     "eth-utils==5.3.1",
#     "pycryptodome==3.23.0",
#     "rich==15.0.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-11-17T00:00:00Z"
# ///
"""Batchers CLI: read when each confidential vault batcher dispatches, from chain.

A vault's address book (`<network>.json`) lists, per vault slug, one deposit
batcher and a rotation of redeem batchers (`redeemBatchers`). For each batcher
this reads, in JSON-RPC batches: paused, owner, the policy min batch age, the
current batch id, the current batch's creation time and pinned min age, and the
previous batch's state and dispatch time.

A batcher dispatches its current batch once all of these hold:
- it is not paused;
- the batch is `batchCreatedAt + batchMinBatchAge` old (the age is pinned when
  the batch opens, so `setMinBatchAge` only moves the next batch);
- its previous batch is no longer `Dispatched` (one batch in flight at a time).

`waiting` names the first of these that does not hold, or `due`.

Output conventions:
- default: one line per batcher, the batcher address tab-separated last
- --json:  normalized records (UTC ISO times, seconds, state names)
- --raw:   the decoded call results per batcher (debugging only)
- --pretty: a table per vault for a human at a terminal
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import requests
from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector, is_address, to_checksum_address
from rich.cells import cell_len
from rich.console import Console
from rich.table import Table
from rich.text import Text

log = logging.getLogger("batchers-cli")



def config_path() -> Path:
    """`$BATCHERS_CLI_CONFIG`, else `~/.config/batchers-cli/config.json`."""
    return Path(os.environ.get("BATCHERS_CLI_CONFIG") or Path.home() / ".config" / "batchers-cli" / "config.json")

# Public nodes that answer JSON-RPC batches without a key.
DEFAULT_RPC = {
    "mainnet": "https://ethereum-rpc.publicnode.com",
    "sepolia": "https://ethereum-sepolia-rpc.publicnode.com",
}
# Where a checkout keeps each network's address book.
ADDRESS_BOOK_PATH = "contracts/deployments/{network}.json"
TIMEOUT_SECONDS = 20
RETRIES = 3
# publicnode answers 429 to bursts of 100-call batches; 40 passes.
CALLS_PER_REQUEST = 40
# Block explorer per chain id, for --pretty links.
EXPLORERS = {1: "https://etherscan.io", 11155111: "https://sepolia.etherscan.io"}
WAITING_STYLES = {"due": "green", "age": "", "previous-batch": "yellow", "paused": "red"}
STATE_STYLES = {"Pending": "cyan", "Dispatched": "yellow", "Finalized": "green", "Canceled": "dim"}
# OZ BatcherConfidential.BatchState, in declaration order.
BATCH_STATES = ("Pending", "Dispatched", "Finalized", "Canceled")

# (field, signature, return types); the batch-scoped ones take a batch id.
BATCHER_CALLS = [
    ("paused", "paused()", ["bool"]),
    ("owner", "owner()", ["address"]),
    ("minBatchAge", "minBatchAge()", ["uint256"]),
    ("currentBatchId", "currentBatchId()", ["uint256"]),
]
BATCH_CALLS = [
    ("state", "batchState(uint256)", ["uint8"]),
    ("createdAt", "batchCreatedAt(uint256)", ["uint256"]),
    ("dispatchedAt", "batchDispatchedAt(uint256)", ["uint256"]),
    ("minAge", "batchMinBatchAge(uint256)", ["uint256"]),
    ("callbackDeadline", "batchCallbackDeadline(uint256)", ["uint256"]),
]


# ---------------------------------------------------------------- config


def configured(*keys: str) -> Any:
    """The value at `keys` in the config file, or None."""
    if not config_path().is_file():
        return None
    value: Any = json.loads(config_path().read_text())
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def reveal(value: str) -> str:
    """`value`, or the secret it names when it is a 1Password reference (`op://…`)."""
    if not value.startswith("op://"):
        return value
    if shutil.which("op") is None:
        raise click.ClickException(f"{config_path()} names {value}, and the 1Password CLI `op` is not installed")
    read = subprocess.run(["op", "read", value], capture_output=True, text=True, check=False)
    if read.returncode != 0:
        raise click.ClickException(f"op read {value} failed: {read.stderr.strip()}")
    return read.stdout.strip()


def resolve_rpc(network: str, flag: str | None) -> str:
    """`--rpc-url`, then `$BATCHERS_RPC_URL`, then the config's `rpc.<network>`, then a public node."""
    value = flag or os.environ.get("BATCHERS_RPC_URL") or configured("rpc", network)
    if value:
        return reveal(value)
    if network in DEFAULT_RPC:
        return DEFAULT_RPC[network]
    raise click.UsageError(
        f"no RPC for network {network}: pass --rpc-url, set BATCHERS_RPC_URL, or add "
        f'{{"rpc": {{"{network}": "<url or op://…>"}}}} to {config_path()}'
    )


def resolve_source(flag: str | None) -> str:
    """`--address-book`, then `$BATCHERS_ADDRESS_BOOK`, then the config's `address_book`."""
    value = flag or os.environ.get("BATCHERS_ADDRESS_BOOK") or configured("address_book")
    if not value:
        raise click.UsageError(
            "no address book: pass --address-book <checkout|file.json|gh:owner/repo[@ref]>, set "
            f'BATCHERS_ADDRESS_BOOK, or add {{"address_book": "gh:owner/repo"}} to {config_path()}'
        )
    return value


def load_address_book(source: str, network: str) -> dict[str, Any]:
    """The address book of `network` from a file, a checkout, or a GitHub repo read through `gh`."""
    rel = ADDRESS_BOOK_PATH.format(network=network)
    if source.startswith("gh:"):
        repo, _, ref = source[3:].partition("@")
        if shutil.which("gh") is None:
            raise click.ClickException(f"{source} is read with the GitHub CLI `gh`, which is not installed")
        endpoint = f"repos/{repo}/contents/{rel}" + (f"?ref={ref}" if ref else "")
        out = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github.raw", endpoint],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            raise click.ClickException(f"gh api {endpoint} failed: {out.stderr.strip()} (run `gh auth status`)")
        text = out.stdout
    else:
        path = Path(source).expanduser()
        if path.is_dir():
            path = path / rel
        if not path.is_file():
            raise click.ClickException(f"no address book at {path}; pass a checkout, a <network>.json or gh:owner/repo")
        text = path.read_text()
    try:
        book = json.loads(text)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"address book for {network} is not JSON: {exc}") from exc
    if not isinstance(book.get("vaults"), dict):
        raise click.ClickException(f"address book for {network} has no `vaults` object")
    return book


def batchers_of(book: dict[str, Any]) -> list[dict[str, str]]:
    """Every batcher in the address book, by vault slug, deposit first then the redeem rotation."""
    rows = []
    for slug in sorted(book["vaults"]):
        vault = book["vaults"][slug]
        if vault.get("depositBatcher"):
            rows.append({"vault": slug, "side": "deposit", "address": to_checksum_address(vault["depositBatcher"])})
        for i, address in enumerate(vault.get("redeemBatchers", [])):
            rows.append({"vault": slug, "side": f"redeem-{i}", "address": to_checksum_address(address)})
    return rows


# ---------------------------------------------------------------- rpc


def retry_after(header: str | None, default: float) -> float:
    """Seconds from a `Retry-After` header, or `default`."""
    try:
        return float(header) if header else default
    except ValueError:
        return default


class Rpc:
    """JSON-RPC over one session: batched `eth_call`s, bounded retries on 429/5xx."""

    def __init__(self, url: str, session: requests.Session | None = None):
        self.url = url
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = "batchers-cli"

    def post(self, payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """One batch POST, retried on 429/5xx honoring `Retry-After`."""
        for attempt in range(1, RETRIES + 1):
            try:
                resp = self.session.post(self.url, json=payload, timeout=TIMEOUT_SECONDS)
            except requests.RequestException as exc:
                if attempt == RETRIES:
                    raise click.ClickException(f"RPC unreachable: {type(exc).__name__}; check --rpc-url") from exc
                time.sleep(2**attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == RETRIES:
                    raise click.ClickException(f"RPC {resp.status_code} after {RETRIES} tries; pass another --rpc-url")
                wait = retry_after(resp.headers.get("Retry-After"), default=2**attempt)
                log.warning("RPC %s, retrying in %ss", resp.status_code, wait)
                time.sleep(wait)
                continue
            if not resp.ok:
                raise click.ClickException(f"RPC {resp.status_code}: {resp.text[:200]}")
            answer = resp.json()
            if not isinstance(answer, list):
                raise click.ClickException(f"RPC refused the batch: {str(answer)[:200]}")
            return answer
        raise AssertionError("unreachable")

    def call_many(self, calls: list[tuple[str, str]]) -> list[str | None]:
        """`eth_call` each (to, data) at latest; a revert answers None."""
        results: list[str | None] = []
        for start in range(0, len(calls), CALLS_PER_REQUEST):
            chunk = calls[start : start + CALLS_PER_REQUEST]
            payload = [
                {"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]}
                for i, (to, data) in enumerate(chunk)
            ]
            by_id = {item["id"]: item for item in self.post(payload)}
            for i in range(len(chunk)):
                item = by_id.get(i, {})
                results.append(item.get("result") if "error" not in item else None)
        return results

    def chain_id(self) -> int:
        """The node's chain id."""
        answer = self.post([{"jsonrpc": "2.0", "id": 0, "method": "eth_chainId", "params": []}])
        return int(answer[0]["result"], 16)


def calldata(signature: str, args: tuple[int, ...] = ()) -> str:
    """ABI calldata for `signature` with uint256 `args`."""
    selector = function_signature_to_4byte_selector(signature)
    return "0x" + (selector + encode(["uint256"] * len(args), list(args))).hex()


def decoded(raw: str | None, types: list[str]) -> Any:
    """The single return value of `raw`, or None for a revert or empty answer."""
    if raw is None or raw in ("0x", ""):
        return None
    value = decode(types, bytes.fromhex(raw[2:]))[0]
    return to_checksum_address(value) if types == ["address"] else value


def read_batchers(rpc: Rpc, addresses: list[str]) -> list[dict[str, Any]]:
    """Each batcher's own fields, then its current and previous batch."""
    calls = [(a, calldata(sig)) for a in addresses for _, sig, _ in BATCHER_CALLS]
    raw = rpc.call_many(calls)
    states = []
    for n, address in enumerate(addresses):
        chunk = raw[n * len(BATCHER_CALLS) : (n + 1) * len(BATCHER_CALLS)]
        fields = {name: decoded(r, types) for (name, _, types), r in zip(BATCHER_CALLS, chunk, strict=True)}
        if fields["currentBatchId"] is None:
            raise click.ClickException(f"{address} answers no currentBatchId(); is it a batcher on this network?")
        states.append({"address": address, **fields})

    batch_reads = []
    for s in states:
        current = s["currentBatchId"]
        for batch_id in (current, current - 1) if current > 1 else (current,):
            batch_reads.append((s["address"], batch_id))
    batches = read_batches(rpc, batch_reads)
    for s in states:
        s["current"] = batches[(s["address"], s["currentBatchId"])]
        s["previous"] = batches.get((s["address"], s["currentBatchId"] - 1))
    return states


def read_batches(rpc: Rpc, reads: list[tuple[str, int]]) -> dict[tuple[str, int], dict[str, Any]]:
    """Each (batcher, batch id)'s state and policy."""
    calls = [(a, calldata(sig, (b,))) for a, b in reads for _, sig, _ in BATCH_CALLS]
    raw = rpc.call_many(calls)
    out = {}
    for n, (address, batch_id) in enumerate(reads):
        chunk = raw[n * len(BATCH_CALLS) : (n + 1) * len(BATCH_CALLS)]
        fields = {name: decoded(r, types) for (name, _, types), r in zip(BATCH_CALLS, chunk, strict=True)}
        state = fields.pop("state")
        out[(address, batch_id)] = {"id": batch_id, "state": BATCH_STATES[state] if state is not None else None, **fields}
    return out


# ---------------------------------------------------------------- normalize


def iso(ts: int | None) -> str | None:
    """UTC ISO-8601 of a unix time; None for unset (0)."""
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def waiting_on(state: dict[str, Any], now: int) -> str:
    """The first condition keeping the current batch from dispatching, or `due`."""
    if state["paused"]:
        return "paused"
    previous = state["previous"]
    if previous and previous["state"] == "Dispatched":
        return "previous-batch"
    if state["nextDispatchAt"] > now:
        return "age"
    return "due"


def normalize(entry: dict[str, str], state: dict[str, Any], labels: dict[str, str], now: int) -> dict[str, Any]:
    """One batcher record for --json and the text line."""
    current, previous = state["current"], state["previous"]
    record = {
        "vault": entry["vault"],
        "side": entry["side"],
        "address": state["address"],
        "owner": state["owner"],
        "ownerLabel": labels.get(state["owner"].lower()) if state["owner"] else None,
        "paused": state["paused"],
        "minBatchAge": state["minBatchAge"],
        "batchId": state["currentBatchId"],
        "batchCreatedAt": iso(current["createdAt"]),
        "batchMinBatchAge": current["minAge"],
        "nextDispatchAt": iso(current["createdAt"] + current["minAge"]),
        "previousBatchId": previous["id"] if previous else None,
        "previousState": previous["state"] if previous else None,
        "previousDispatchedAt": iso(previous["dispatchedAt"]) if previous else None,
    }
    record["waiting"] = waiting_on(
        {**state, "nextDispatchAt": current["createdAt"] + current["minAge"]}, now
    )
    return record


def duration(seconds: int | None) -> str:
    """`1d02h`, `23h58m`, `30m` or `45s`."""
    if seconds is None:
        return "-"
    sign, seconds = ("-", -seconds) if seconds < 0 else ("", seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{sign}{days}d{hours:02d}h"
    if hours:
        return f"{sign}{hours}h{minutes:02d}m"
    if minutes:
        return f"{sign}{minutes}m"
    return f"{sign}{rest}s"


def relative(seconds: int | None) -> str:
    """`in 4m` ahead of now, `13s ago` behind it."""
    if seconds is None:
        return "-"
    return f"in {duration(seconds)}" if seconds >= 0 else f"{duration(-seconds)} ago"


def short_time(value: str | None) -> str:
    """`2026-11-18 11:00 UTC` from an ISO time."""
    return f"{value[:10]} {value[11:16]} UTC" if value else "-"


def seconds_until(value: str | None, now: int) -> int | None:
    """Seconds from `now` to an ISO UTC time; negative once it has passed."""
    if not value:
        return None
    return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()) - now


def format_line(record: dict[str, Any], now: int) -> str:
    """The compact one-line view; the full batcher address is tab-separated last."""
    next_at = record["nextDispatchAt"]
    eta = seconds_until(next_at, now)
    owner = record["ownerLabel"] or record["owner"]
    previous = (
        f"prev #{record['previousBatchId']} {record['previousState']} {short_time(record['previousDispatchedAt'])}"
        if record["previousBatchId"]
        else "prev -"
    )
    parts = [
        f"{record['vault']} {record['side']}",
        f"#{record['batchId']}",
        f"age {duration(record['batchMinBatchAge'])} (policy {duration(record['minBatchAge'])})",
        f"next {short_time(next_at)} ({relative(eta)})",
        f"waiting {record['waiting']}",
        previous,
        f"owner {owner}",
    ]
    return "  ".join(parts) + f"\t{record['address']}"


def side_order(side: str) -> tuple[int, int]:
    """deposit first, then redeem-0, redeem-1, … in rotation order."""
    return (0, 0) if side == "deposit" else (1, int(side.rsplit("-", 1)[-1]) if side[-1].isdigit() else 0)


def linked(address: str | None, chain_id: int | None, label: str | None = None) -> str:
    """`label` or a short address, linked to its explorer page when the chain has one."""
    if not address:
        return "-"
    text = label or f"{address[:6]}…{address[-4:]}"
    base = EXPLORERS.get(chain_id or 0)
    return f"[link={base}/address/{address}]{text}[/link]" if base else text


def styled(value: str | None, styles: dict[str, str]) -> str:
    """`value` in its style, or `-`."""
    if not value:
        return "-"
    style = styles.get(value, "")
    return f"[{style}]{value}[/]" if style else value


def table_of(title: str, columns: list[str], rows: list[list[str]], right: set[str]) -> Table:
    """A table whose columns are never narrower than their longest cell.

    Below that width the terminal soft-wraps lines instead of Rich truncating cells.
    """
    table = Table(title=title, title_justify="left", header_style="bold")
    for index, column in enumerate(columns):
        width = max(cell_len(Text.from_markup(cell).plain) for cell in [column, *(row[index] for row in rows)])
        table.add_column(column, no_wrap=True, min_width=width, justify="right" if column in right else "left")
    for row in rows:
        table.add_row(*row)
    return table


def render_schedule(records: list[dict[str, Any]], chain_id: int | None, now: int, console: Console | None = None) -> None:
    """One table per vault, its batchers in rotation order."""
    console = console or Console()
    console.print(f"[dim]now {short_time(iso(now))}[/dim]")
    by_vault: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_vault.setdefault(record["vault"], []).append(record)
    for vault, group in sorted(by_vault.items()):
        rows = []
        for r in sorted(group, key=lambda r: side_order(r["side"])):
            previous = (
                f"#{r['previousBatchId']} {styled(r['previousState'], STATE_STYLES)}" if r["previousBatchId"] else "-"
            )
            age = duration(r["batchMinBatchAge"])
            rows.append([
                r["side"],
                f"#{r['batchId']}",
                styled(r["waiting"], WAITING_STYLES),
                short_time(r["nextDispatchAt"]).removesuffix(" UTC"),
                relative(seconds_until(r["nextDispatchAt"], now)),
                age,
                age if r["minBatchAge"] == r["batchMinBatchAge"] else f"[yellow]{duration(r['minBatchAge'])}[/yellow]",
                previous,
                linked(r["owner"], chain_id, r["ownerLabel"]),
                linked(r["address"], chain_id),
            ])
        columns = ["Side", "Batch", "Waiting", "Next (UTC)", "When", "Age", "Policy", "Previous", "Owner", "Batcher"]
        table = table_of(f"[bold]{vault}[/bold]", columns, rows, {"Batch", "Age", "Policy"})
        console.print(table, crop=False)
        console.print()


def render_batchers(records: list[dict[str, Any]], chain_id: int | None, now: int, console: Console | None = None) -> None:
    """One table per batcher: its schedule line as the title, its last batches as rows."""
    console = console or Console()
    for r in records:
        title = (
            f"[bold]{r['vault']} {r['side']}[/bold] · {linked(r['address'], chain_id, r['address'])} · "
            f"waiting {styled(r['waiting'], WAITING_STYLES)} · next {short_time(r['nextDispatchAt'])} "
            f"({relative(seconds_until(r['nextDispatchAt'], now))}) · owner {linked(r['owner'], chain_id, r['ownerLabel'])}"
        )
        rows = []
        for b in r["batches"]:
            rows.append([
                f"#{b['id']}",
                styled(b["state"], STATE_STYLES),
                short_time(b["createdAt"]).removesuffix(" UTC"),
                duration(b["minBatchAge"]),
                short_time(b["dispatchedAt"]).removesuffix(" UTC"),
                duration(b["callbackDeadline"]),
            ])
        columns = ["Batch", "State", "Created (UTC)", "Age", "Dispatched (UTC)", "Callback deadline"]
        console.print(title)
        console.print(table_of("", columns, rows, {"Batch", "Age", "Callback deadline"}), crop=False)
        console.print()


def emit_json(value: Any) -> None:
    """JSON on stdout."""
    click.echo(json.dumps(value, indent=1, default=str))


# ---------------------------------------------------------------- cli


def network_context(ctx: click.Context) -> tuple[dict[str, Any], Rpc]:
    """The address book and an RPC client checked to serve the address book's chain."""
    obj = ctx.obj
    book = load_address_book(resolve_source(obj["address_book"]), obj["network"])
    rpc = Rpc(resolve_rpc(obj["network"], obj["rpc_url"]))
    expected = book.get("chainId")
    actual = rpc.chain_id()
    if expected is not None and int(expected) != actual:
        raise click.ClickException(f"RPC serves chain {actual}, the {obj['network']} address book chain {expected}; fix --rpc-url")
    return book, rpc


def labels() -> dict[str, str]:
    """Owner labels from the config's `labels` ({address: name}), keys lower-cased."""
    raw = configured("labels") or {}
    return {k.lower(): v for k, v in raw.items()} if isinstance(raw, dict) else {}


@click.group(
    epilog="\b\nExamples:\n  batchers schedule\n  batchers schedule --vault curator-usdc --json\n  batchers batcher curator-usdc/redeem-2 --history 5"
)
@click.option("--network", default="mainnet", show_default=True, help="Address book to read: mainnet or sepolia.")
@click.option("--address-book", help="Checkout dir, <network>.json file, or gh:owner/repo[@ref]. Default: config.")
@click.option("--rpc-url", help="JSON-RPC URL. Default: $BATCHERS_RPC_URL, config `rpc.<network>`, a public node.")
@click.option("--debug", is_flag=True, help="Log to stderr at DEBUG.")
@click.pass_context
def cli(ctx: click.Context, network: str, address_book: str | None, rpc_url: str | None, debug: bool) -> None:
    """When each confidential vault batcher dispatches, read from chain."""
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if debug else logging.WARNING, format="%(levelname)s %(message)s")
    ctx.obj = {"network": network, "address_book": address_book, "rpc_url": rpc_url}


@cli.command(
    epilog="\b\nExamples:\n  batchers vaults\n  batchers vaults --json\n  batchers --network sepolia vaults"
)
@click.option("--json", "as_json", is_flag=True, help="Normalized records.")
@click.pass_context
def vaults(ctx: click.Context, as_json: bool) -> None:
    """Each vault in the address book with its batchers (no chain read)."""
    obj = ctx.obj
    book = load_address_book(resolve_source(obj["address_book"]), obj["network"])
    rows = batchers_of(book)
    if as_json:
        emit_json(rows)
        return
    for row in rows:
        click.echo(f"{row['vault']} {row['side']}\t{row['address']}")


@cli.command(
    epilog="\b\nExamples:\n"
    "  batchers schedule\n"
    "  batchers schedule --vault curator-usdc --vault curator-usdt\n"
    "  batchers schedule --side redeem --waiting previous-batch\n"
    "  batchers schedule --pretty\n"
    "  batchers schedule --json | jq '.[] | select(.waiting == \"due\")'"
)
@click.option("--vault", "slugs", multiple=True, help="Vault slug. Repeatable. Default: every vault.")
@click.option("--side", type=click.Choice(["deposit", "redeem"]), help="Only deposit or redeem batchers.")
@click.option(
    "--waiting",
    type=click.Choice(["paused", "previous-batch", "age", "due"]),
    help="Only batchers waiting on this (client-side).",
)
@click.option("--limit", type=int, default=0, help="Show at most N batchers, soonest dispatch first. 0: all.")
@click.option("--json", "as_json", is_flag=True, help="Normalized records.")
@click.option("--raw", is_flag=True, help="Decoded call results per batcher (debugging).")
@click.option("--pretty", is_flag=True, help="A table per vault for a human: colours, linked short addresses.")
@click.pass_context
def schedule(
    ctx: click.Context,
    slugs: tuple[str, ...],
    side: str | None,
    waiting: str | None,
    limit: int,
    as_json: bool,
    raw: bool,
    pretty: bool,
) -> None:
    """Each batcher's current batch, when it can dispatch, and what it waits on."""
    book, rpc = network_context(ctx)
    entries = batchers_of(book)
    unknown = sorted(set(slugs) - {e["vault"] for e in entries})
    if unknown:
        raise click.BadParameter(f"{', '.join(unknown)} not in the address book; list them with `batchers vaults`", param_hint="--vault")
    entries = [e for e in entries if (not slugs or e["vault"] in slugs) and (not side or e["side"].startswith(side))]
    states = read_batchers(rpc, [e["address"] for e in entries])
    if raw:
        emit_json([{**e, **s} for e, s in zip(entries, states, strict=True)])
        return
    now = int(time.time())
    names = labels()
    records = [normalize(e, s, names, now) for e, s in zip(entries, states, strict=True)]
    if waiting:
        records = [r for r in records if r["waiting"] == waiting]
    records.sort(key=lambda r: (r["nextDispatchAt"] or "", r["vault"], r["side"]))
    if limit:
        records = records[:limit]
    if as_json:
        emit_json(records)
        return
    if pretty:
        render_schedule(records, book.get("chainId"), now)
        return
    click.echo(f"now {short_time(iso(now))}  network {ctx.obj['network']}")
    for record in records:
        click.echo(format_line(record, now))


def resolve_ref(ref: str, entries: list[dict[str, str]]) -> dict[str, str]:
    """A batcher by address or `<vault>/<side>`; an unknown one lists the candidates."""
    if is_address(ref):
        address = to_checksum_address(ref)
        match = [e for e in entries if e["address"] == address]
        return match[0] if match else {"vault": "-", "side": "-", "address": address}
    vault, _, side = ref.partition("/")
    match = [e for e in entries if e["vault"] == vault and e["side"] == side]
    if not match:
        sides = [e["side"] for e in entries if e["vault"] == vault]
        hint = f"sides of {vault}: {', '.join(sides)}" if sides else "list them with `batchers vaults`"
        raise click.BadParameter(f"{ref} is no batcher; {hint}", param_hint="REF")
    return match[0]


@cli.command(
    epilog="\b\nExamples:\n"
    "  batchers batcher curator-usdc/redeem-2\n"
    "  batchers batcher curator-usdc/redeem-2 --history 10 --pretty\n"
    "  batchers batcher curator-usdt/deposit curator-usdt/redeem-0 --history 5 --json\n"
    "  batchers batcher 0x0000000000000000000000000000000000001234"
)
@click.argument("refs", nargs=-1, required=True)
@click.option("--history", type=int, default=3, show_default=True, help="Batches to show, newest first.")
@click.option("--json", "as_json", is_flag=True, help="Normalized records.")
@click.option("--pretty", is_flag=True, help="A table per batcher for a human: its last batches, colours, links.")
@click.pass_context
def batcher(ctx: click.Context, refs: tuple[str, ...], history: int, as_json: bool, pretty: bool) -> None:
    """One or more batchers in full, with their last batches. REF: address or <vault>/<side>."""
    book, rpc = network_context(ctx)
    entries = [resolve_ref(ref, batchers_of(book)) for ref in refs]
    states = read_batchers(rpc, [e["address"] for e in entries])
    reads = [(s["address"], b) for s in states for b in range(s["currentBatchId"], max(0, s["currentBatchId"] - history), -1)]
    batches = read_batches(rpc, reads)
    now = int(time.time())
    names = labels()
    records = []
    for entry, state in zip(entries, states, strict=True):
        record = normalize(entry, state, names, now)
        record["batches"] = [
            {
                "id": b["id"],
                "state": b["state"],
                "createdAt": iso(b["createdAt"]),
                "minBatchAge": b["minAge"],
                "dispatchedAt": iso(b["dispatchedAt"]),
                "callbackDeadline": b["callbackDeadline"],
            }
            for b in (batches[(state["address"], i)] for i in range(state["currentBatchId"], max(0, state["currentBatchId"] - history), -1))
        ]
        records.append(record)
    if as_json:
        emit_json(records)
        return
    if pretty:
        render_batchers(records, book.get("chainId"), now)
        return
    for record in records:
        click.echo(format_line(record, now))
        for b in record["batches"]:
            click.echo(
                f"  #{b['id']} {b['state']}  created {short_time(b['createdAt'])}  age {duration(b['minBatchAge'])}"
                f"  dispatched {short_time(b['dispatchedAt'])}  callback deadline {duration(b['callbackDeadline'])}"
            )


if __name__ == "__main__":
    cli()
