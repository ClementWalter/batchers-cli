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

A dispatched batch unwraps its deposits; the input wrapper's `UnwrapFinalized`
log, keyed by the batch's `unwrapRequestId`, carries the cleartext amount the
batch unshielded. The batcher's `Joined`/`Quit` logs before its `BatchDispatched`
give the batch's depositors. Nodes that prune logs leave older batches' amounts unknown.
The amount is shown in the wrapper's underlying token, and in USD at the current
price: DefiLlama's price of the underlying, or for an ERC-4626 share its on-chain
`convertToAssets` times DefiLlama's price of the asset.

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
from decimal import Decimal
from pathlib import Path
from typing import Any

import click
import requests
from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector, is_address, keccak, to_checksum_address
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
# IERC7984ERC20Wrapper.UnwrapFinalized(receiver, unwrapRequestId, encryptedAmount, cleartextAmount).
UNWRAP_FINALIZED = "0x" + keccak(text="UnwrapFinalized(address,bytes32,bytes32,uint64)").hex()
# BatcherConfidential: a deposit, a withdrawal while Pending (or refund once Canceled), and the dispatch.
JOINED = "0x" + keccak(text="Joined(uint256,address,bytes32)").hex()
QUIT = "0x" + keccak(text="Quit(uint256,address,bytes32)").hex()
BATCH_DISPATCHED = "0x" + keccak(text="BatchDispatched(uint256)").hex()
# Blocks per eth_getLogs; publicnode serves logs only for its last ~10k blocks.
LOG_BLOCKS = 5000
# DefiLlama's keyless current-price API, and its name for each chain id.
PRICES_URL = "https://coins.llama.fi/prices/current/{coins}"
PRICE_CHAINS = {1: "ethereum"}
# Post-merge slot time: a timestamp's block is at most this many seconds per block back.
SECONDS_PER_BLOCK = 12

# (field, signature, return types); the batch-scoped ones take a batch id.
BATCHER_CALLS = [
    ("paused", "paused()", ["bool"]),
    ("owner", "owner()", ["address"]),
    ("minBatchAge", "minBatchAge()", ["uint256"]),
    ("currentBatchId", "currentBatchId()", ["uint256"]),
    ("fromToken", "fromToken()", ["address"]),
]
BATCH_CALLS = [
    ("state", "batchState(uint256)", ["uint8"]),
    ("createdAt", "batchCreatedAt(uint256)", ["uint256"]),
    ("dispatchedAt", "batchDispatchedAt(uint256)", ["uint256"]),
    ("minAge", "batchMinBatchAge(uint256)", ["uint256"]),
    ("callbackDeadline", "batchCallbackDeadline(uint256)", ["uint256"]),
    ("unwrapRequestId", "unwrapRequestId(uint256)", ["bytes32"]),
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


def rate_limited(error: dict[str, Any] | None) -> bool:
    """Whether a per-call JSON-RPC error is a rate limit (QuickNode -32007, the -32005 convention)."""
    if not error:
        return False
    message = str(error.get("message", "")).lower()
    return error.get("code") in (-32005, -32007, 429) or "rate limit" in message or "request limit" in message


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
                # publicnode answers 403 with per-call errors when every call of a batch asks for pruned logs.
                try:
                    answer = resp.json()
                except ValueError:
                    answer = None
                if isinstance(answer, list):
                    return answer
                raise click.ClickException(f"RPC {resp.status_code}: {resp.text[:200]}")
            answer = resp.json()
            if not isinstance(answer, list):
                raise click.ClickException(f"RPC refused the batch: {str(answer)[:200]}")
            return answer
        raise AssertionError("unreachable")

    def batch(self, method: str, params: list[list[Any]], errors: dict[int, str] | None = None) -> list[Any]:
        """`method` with each params, in batches; a call the node refuses answers None, its message in `errors`.

        Rate-limited calls are retried, never answered None: a None must mean the node refused that call.
        """
        results: list[Any] = [None] * len(params)
        todo = list(range(len(params)))
        for attempt in range(1, RETRIES + 2):
            limited = []
            for start in range(0, len(todo), CALLS_PER_REQUEST):
                chunk = todo[start : start + CALLS_PER_REQUEST]
                payload = [{"jsonrpc": "2.0", "id": i, "method": method, "params": params[i]} for i in chunk]
                by_id = {item.get("id"): item for item in self.post(payload)}
                for i in chunk:
                    item = by_id.get(i)
                    if item is None or rate_limited(item.get("error")):
                        limited.append(i)
                    elif "error" in item:
                        log.debug("%s %s: %s", method, params[i], item["error"])
                        if errors is not None:
                            errors[i] = str(item["error"].get("message", item["error"]))
                    else:
                        results[i] = item.get("result")
            if not limited:
                return results
            if attempt > RETRIES:
                raise click.ClickException(f"RPC rate-limited {len(limited)} {method} call(s) after {RETRIES} retries; retry later")
            log.info("RPC rate-limited %d %s call(s), retrying in %ss", len(limited), method, 2**attempt)
            time.sleep(2**attempt)
            todo = limited
        raise AssertionError("unreachable")

    def call_many(self, calls: list[tuple[str, str]]) -> list[str | None]:
        """`eth_call` each (to, data) at latest; a revert answers None."""
        return self.batch("eth_call", [[{"to": to, "data": data}, "latest"] for to, data in calls])

    def latest_block(self) -> tuple[int, int]:
        """The latest block's number and timestamp."""
        block = self.post([{"jsonrpc": "2.0", "id": 0, "method": "eth_getBlockByNumber", "params": ["latest", False]}])[0]["result"]
        return int(block["number"], 16), int(block["timestamp"], 16)

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
    if types == ["address"]:
        return to_checksum_address(value)
    return "0x" + value.hex() if types == ["bytes32"] else value


def read_batchers(rpc: Rpc, addresses: list[str], with_logs: bool = True) -> list[dict[str, Any]]:
    """Each batcher's own fields, then its current and previous batch (with their logged unshielded and depositors)."""
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
    if with_logs:
        read_batch_logs(rpc, batches, {s["address"]: s["fromToken"] for s in states})
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


def read_batch_logs(rpc: Rpc, batches: dict[tuple[str, int], dict[str, Any]], from_tokens: dict[str, str | None]) -> None:
    """Set each batch's `unshielded` and `depositors` from logs, in one `eth_getLogs` batch.

    - `unshielded`: the cleartext of the input wrapper's `UnwrapFinalized` log for the batch's `unwrapRequestId`;
      None while the unwrap is not finalized.
    - `depositors`: accounts in the batch, from its `Joined` and `Quit` logs up to its `BatchDispatched` log
      (a later `Quit` is a refund of a canceled batch, not a leave).

    `unshieldedUnknown` / `depositorsUnknown` are True where the node refused the logs the answer sits in.
    """
    for batch in batches.values():
        batch.update(unshielded=None, unshieldedUnknown=False, depositors=None, depositorsUnknown=False)
    unwraps = [
        (from_tokens[address], batch)
        for (address, _), batch in batches.items()
        if from_tokens.get(address) and batch.get("unwrapRequestId") and int(batch["unwrapRequestId"], 16)
        and batch.get("dispatchedAt")
    ]
    joins = [(address, batch) for (address, _), batch in batches.items() if batch.get("createdAt")]
    if not unwraps and not joins:
        return
    latest, latest_ts = rpc.latest_block()

    def block_at(ts: int) -> int:
        # Missed slots only make the real block later, so this never starts past it.
        return max(0, latest - -(-(latest_ts - ts) // SECONDS_PER_BLOCK))

    # (address, topics, first block) per query, chunked to LOG_BLOCKS up to latest.
    queries: list[tuple[str, list[Any], int]] = []
    by_token: dict[str, list[dict[str, Any]]] = {}
    for token, batch in unwraps:
        by_token.setdefault(token, []).append(batch)
    for token, group in by_token.items():
        ids = sorted({b["unwrapRequestId"] for b in group})
        queries.append((token, [UNWRAP_FINALIZED, None, ids], min(block_at(b["dispatchedAt"]) for b in group)))
    by_batcher: dict[str, list[dict[str, Any]]] = {}
    for address, batch in joins:
        by_batcher.setdefault(address, []).append(batch)
    for address, group in by_batcher.items():
        ids = ["0x" + encode(["uint256"], [b["id"]]).hex() for b in group]
        queries.append((address, [[JOINED, QUIT, BATCH_DISPATCHED], ids], min(block_at(b["createdAt"]) for b in group)))
    filters = [
        (address, end, {"address": address, "fromBlock": hex(start), "toBlock": hex(end), "topics": topics})
        for address, topics, first in queries
        for start in range(first, latest + 1, LOG_BLOCKS)
        for end in (min(start + LOG_BLOCKS - 1, latest),)
    ]
    reasons: dict[int, str] = {}
    answers = rpc.batch("eth_getLogs", [[f] for *_, f in filters], reasons)
    logs: dict[str, list[dict[str, Any]]] = {}
    refused: dict[str, int] = {}
    for (address, end, _), answer in zip(filters, answers, strict=True):
        if answer is None:
            refused[address] = max(refused.get(address, -1), end)
        else:
            logs.setdefault(address, []).extend(answer)

    unwrapped = {
        (token, entry["topics"][2].lower()): int(entry["data"][-64:], 16)  # data = encrypted ‖ cleartext uint64
        for token in by_token
        for entry in logs.get(token, [])
    }
    for token, batch in unwraps:
        batch["unshielded"] = unwrapped.get((token, batch["unwrapRequestId"].lower()))
        batch["unshieldedUnknown"] = batch["unshielded"] is None and block_at(batch["dispatchedAt"]) <= refused.get(token, -1)

    for address, group in by_batcher.items():
        events = sorted(logs.get(address, []), key=lambda e: (int(e["blockNumber"], 16), int(e["logIndex"], 16)))
        for batch in group:
            if block_at(batch["createdAt"]) <= refused.get(address, -1):
                batch["depositorsUnknown"] = True
                continue
            members: set[str] = set()
            for entry in events:
                if int(entry["topics"][1], 16) != batch["id"]:
                    continue
                if entry["topics"][0] == BATCH_DISPATCHED:
                    break
                account = entry["topics"][2][-40:].lower()
                (members.add if entry["topics"][0] == JOINED else members.discard)(account)
            batch["depositors"] = len(members)

    unknown = sum(b["unshieldedUnknown"] or b["depositorsUnknown"] for b in batches.values())
    if unknown:
        log.warning(
            "the RPC refused logs (%s): %d batch(es) partly unknown; pass an archive --rpc-url",
            next(iter(reasons.values()), "no reason"),
            unknown,
        )


def read_fields(rpc: Rpc, calls: list[tuple[str, str, str, tuple[int, ...], list[str]]]) -> dict[tuple[str, str], Any]:
    """{(address, field): value} for each (address, field, signature, args, types); a revert is None."""
    raw = rpc.call_many([(address, calldata(sig, args)) for address, _, sig, args, _ in calls])
    out = {}
    for (address, field, _, _, types), r in zip(calls, raw, strict=True):
        try:
            out[(address, field)] = decoded(r, types)
        except Exception:  # noqa: BLE001 - a token answering a non-standard shape (e.g. bytes32 symbol) is unknown
            out[(address, field)] = None
    return out


def usd_prices(chain_id: int | None, tokens: list[str]) -> dict[str, float]:
    """DefiLlama's current USD price per whole token, keyed by lower-cased address; {} when unavailable."""
    chain = PRICE_CHAINS.get(chain_id or 0)
    if not chain or not tokens:
        return {}
    coins = ",".join(f"{chain}:{t}" for t in sorted({t.lower() for t in tokens}))
    try:
        resp = requests.get(PRICES_URL.format(coins=coins), timeout=TIMEOUT_SECONDS, headers={"User-Agent": "batchers-cli"})
        resp.raise_for_status()
        answer = resp.json()["coins"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning("no USD prices from DefiLlama (%s)", type(exc).__name__)
        return {}
    return {key.split(":", 1)[1].lower(): float(value["price"]) for key, value in answer.items()}


def read_tokens(rpc: Rpc, chain_id: int | None, wrappers: list[str]) -> dict[str, dict[str, Any]]:
    """Per input wrapper: its underlying token's address, symbol and decimals, the wrapper `rate`, and `priceUsd`.

    `priceUsd` is per whole underlying token: DefiLlama's, or for an ERC-4626 share `convertToAssets` × the asset's.
    """
    wrappers = sorted(set(wrappers))
    w = read_fields(rpc, [
        c for t in wrappers for c in (
            (t, "underlying", "underlying()", (), ["address"]),
            (t, "rate", "rate()", (), ["uint256"]),
        )
    ])
    underlyings = sorted({w[(t, "underlying")] for t in wrappers if w[(t, "underlying")]})
    u = read_fields(rpc, [
        c for t in underlyings for c in (
            (t, "symbol", "symbol()", (), ["string"]),
            (t, "decimals", "decimals()", (), ["uint8"]),
            (t, "asset", "asset()", (), ["address"]),
        )
    ])
    shares = [t for t in underlyings if u[(t, "asset")] and u[(t, "decimals")] is not None]
    v = read_fields(rpc, [
        c for t in shares for c in (
            (t, "perShare", "convertToAssets(uint256)", (10 ** u[(t, "decimals")],), ["uint256"]),
            (u[(t, "asset")], "decimals", "decimals()", (), ["uint8"]),
        )
    ])
    prices = usd_prices(chain_id, [u[(t, "asset")] if t in shares else t for t in underlyings])

    def price_of(token: str) -> float | None:
        if token not in shares:
            return prices.get(token.lower())
        asset = u[(token, "asset")]
        per_share, asset_decimals, asset_price = v[(token, "perShare")], v[(asset, "decimals")], prices.get(asset.lower())
        if per_share is None or asset_decimals is None or asset_price is None:
            return None
        return float(Decimal(per_share).scaleb(-asset_decimals)) * asset_price

    out = {}
    for t in wrappers:
        underlying = w[(t, "underlying")]
        out[t] = {
            "address": underlying,
            "symbol": u.get((underlying, "symbol")),
            "decimals": u.get((underlying, "decimals")),
            "rate": w[(t, "rate")],
            "priceUsd": price_of(underlying) if underlying else None,
        }
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


def amount(raw: int | None, token: dict[str, Any] | None) -> str | None:
    """`raw` wrapper units as whole underlying tokens (`raw × rate / 10^decimals`), e.g. `1234.5`.

    Raw wrapper units when the underlying is unknown.
    """
    if raw is None:
        return None
    if not token or token.get("decimals") is None or token.get("rate") is None:
        return str(raw)
    text = f"{Decimal(raw * token['rate']).scaleb(-token['decimals']):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def unshielded_of(batch: dict[str, Any] | None, token: dict[str, Any] | None) -> str | None:
    """A batch's unshielded amount in whole underlying tokens, `?` when the node refused its logs."""
    if not batch:
        return None
    return "?" if batch.get("unshieldedUnknown") else amount(batch.get("unshielded"), token)


def depositors_of(batch: dict[str, Any] | None) -> int | str | None:
    """A batch's depositor count, `?` when the node refused its logs."""
    if not batch:
        return None
    return "?" if batch.get("depositorsUnknown") else batch.get("depositors")


def usd_of(whole: str | None, token: dict[str, Any] | None) -> float | None:
    """The USD value of `whole` underlying tokens at the current price, to the cent."""
    if whole in (None, "?") or not token or token.get("priceUsd") is None or token.get("decimals") is None:
        return None
    return round(float(Decimal(whole)) * token["priceUsd"], 2)


def units(whole: str | None, token: dict[str, Any] | None) -> str:
    """`1,602,071.31 USDT`: thousands separators, cents above one token, up to 6 decimals below."""
    if whole in (None, "?"):
        return whole or "-"
    value = Decimal(whole)
    if value == 0:
        text = "0"
    elif abs(value) >= 1:
        text = f"{value:,.2f}"
    else:
        text = f"{value:,.6f}".rstrip("0").rstrip(".")
    symbol = (token or {}).get("symbol")
    return f"{text} {symbol}" if symbol else text


def count(value: int | str | None) -> str:
    """A count, `?` unknown, `-` none."""
    return "-" if value is None else str(value)


def dollars(value: float | None) -> str:
    """`$1,600,382.17`, or `-`."""
    return "-" if value is None else f"${value:,.2f}"


def unshielded_text(whole: str | None, usd: float | None, token: dict[str, Any] | None) -> str:
    """`1,602,071.31 USDT ($1,600,382.17)` for the text line."""
    return units(whole, token) + (f" ({dollars(usd)})" if usd is not None else "")


def normalize(
    entry: dict[str, str], state: dict[str, Any], labels: dict[str, str], now: int, tokens: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """One batcher record for --json and the text line."""
    current, previous = state["current"], state["previous"]
    token = tokens.get(state["fromToken"]) if state["fromToken"] else None
    previous_unshielded = unshielded_of(previous, token)
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
        "depositors": depositors_of(current),
        "previousDepositors": depositors_of(previous),
        "previousUnshielded": previous_unshielded,
        "previousUnshieldedUsd": usd_of(previous_unshielded, token),
        "previousUnshieldedRaw": previous.get("unshielded") if previous else None,
        "fromToken": state["fromToken"],
        "unshieldedToken": (
            {k: token[k] for k in ("address", "symbol", "decimals", "priceUsd")} if token else None
        ),
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
        f" depositors {count(record['previousDepositors'])}"
        f" unshielded {unshielded_text(record['previousUnshielded'], record['previousUnshieldedUsd'], record['unshieldedToken'])}"
        if record["previousBatchId"]
        else "prev -"
    )
    parts = [
        f"{record['vault']} {record['side']}",
        f"#{record['batchId']} depositors {count(record['depositors'])}",
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
                count(r["depositors"]),
                styled(r["waiting"], WAITING_STYLES),
                short_time(r["nextDispatchAt"]).removesuffix(" UTC"),
                relative(seconds_until(r["nextDispatchAt"], now)),
                age,
                age if r["minBatchAge"] == r["batchMinBatchAge"] else f"[yellow]{duration(r['minBatchAge'])}[/yellow]",
                previous,
                count(r["previousDepositors"]),
                units(r["previousUnshielded"], r["unshieldedToken"]),
                dollars(r["previousUnshieldedUsd"]),
                linked(r["owner"], chain_id, r["ownerLabel"]),
                linked(r["address"], chain_id),
            ])
        columns = [
            "Side", "Batch", "Depositors", "Waiting", "Next (UTC)", "When", "Age", "Policy",
            "Previous", "Prev. depositors", "Unshielded", "USD", "Owner", "Batcher",
        ]
        right = {"Batch", "Depositors", "Age", "Policy", "Prev. depositors", "Unshielded", "USD"}
        table = table_of(f"[bold]{vault}[/bold]", columns, rows, right)
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
                count(b["depositors"]),
                short_time(b["createdAt"]).removesuffix(" UTC"),
                duration(b["minBatchAge"]),
                short_time(b["dispatchedAt"]).removesuffix(" UTC"),
                duration(b["callbackDeadline"]),
                units(b["unshielded"], r["unshieldedToken"]),
                dollars(b["unshieldedUsd"]),
            ])
        columns = ["Batch", "State", "Depositors", "Created (UTC)", "Age", "Dispatched (UTC)", "Callback deadline", "Unshielded", "USD"]
        right = {"Batch", "Depositors", "Age", "Callback deadline", "Unshielded", "USD"}
        console.print(title)
        console.print(table_of("", columns, rows, right), crop=False)
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
    epilog="\b\nExamples:\n  batchers vaults\n  batchers vaults --json\n  batchers vaults --pretty\n  batchers --network sepolia vaults"
)
@click.option("--json", "as_json", is_flag=True, help="Normalized records.")
@click.option("--pretty", is_flag=True, help="A table per vault for a human, addresses linked.")
@click.pass_context
def vaults(ctx: click.Context, as_json: bool, pretty: bool) -> None:
    """Each vault in the address book with its batchers (no chain read)."""
    obj = ctx.obj
    book = load_address_book(resolve_source(obj["address_book"]), obj["network"])
    rows = batchers_of(book)
    if as_json:
        emit_json(rows)
        return
    if pretty:
        console = Console()
        by_vault: dict[str, list[list[str]]] = {}
        for row in rows:
            by_vault.setdefault(row["vault"], []).append([row["side"], linked(row["address"], book.get("chainId"), row["address"])])
        for vault, group in by_vault.items():
            console.print(table_of(f"[bold]{vault}[/bold]", ["Side", "Batcher"], group, set()), crop=False)
            console.print()
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
    tokens = read_tokens(rpc, book.get("chainId"), [s["fromToken"] for s in states if s["fromToken"]])
    records = [normalize(e, s, names, now, tokens) for e, s in zip(entries, states, strict=True)]
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
    states = read_batchers(rpc, [e["address"] for e in entries], with_logs=False)
    reads = [(s["address"], b) for s in states for b in range(s["currentBatchId"], max(0, s["currentBatchId"] - history), -1)]
    batches = read_batches(rpc, reads)
    read_batch_logs(rpc, batches, {s["address"]: s["fromToken"] for s in states})
    for s in states:
        s["current"] = batches[(s["address"], s["currentBatchId"])]
        if s["previous"]:
            s["previous"] = batches[(s["address"], s["previous"]["id"])]
    now = int(time.time())
    names = labels()
    tokens = read_tokens(rpc, book.get("chainId"), [s["fromToken"] for s in states if s["fromToken"]])
    records = []
    for entry, state in zip(entries, states, strict=True):
        record = normalize(entry, state, names, now, tokens)
        token = tokens.get(state["fromToken"]) if state["fromToken"] else None
        record["batches"] = [
            {
                "id": b["id"],
                "state": b["state"],
                "createdAt": iso(b["createdAt"]),
                "minBatchAge": b["minAge"],
                "dispatchedAt": iso(b["dispatchedAt"]),
                "callbackDeadline": b["callbackDeadline"],
                "unwrapRequestId": b["unwrapRequestId"],
                "depositors": depositors_of(b),
                "unshielded": unshielded_of(b, token),
                "unshieldedUsd": usd_of(unshielded_of(b, token), token),
                "unshieldedRaw": b["unshielded"],
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
                f"  #{b['id']} {b['state']}  depositors {count(b['depositors'])}  created {short_time(b['createdAt'])}  age {duration(b['minBatchAge'])}"
                f"  dispatched {short_time(b['dispatchedAt'])}  callback deadline {duration(b['callbackDeadline'])}"
                f"  unshielded {unshielded_text(b['unshielded'], b['unshieldedUsd'], record['unshieldedToken'])}"
            )


if __name__ == "__main__":
    cli()
