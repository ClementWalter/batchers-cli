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
#     "pytest==8.4.2",
# ]
#
# [tool.uv]
# exclude-newer = "2026-11-17T00:00:00Z"
# ///
"""Offline tests for batchers_cli: a stub JSON-RPC node answers the batcher getters."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from eth_abi import decode, encode
from eth_utils import function_signature_to_4byte_selector

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import batchers_cli as bc  # noqa: E402

NOW = 1_800_000_000
DEPOSIT = "0x0000000000000000000000000000000000001234"
REDEEM_0 = "0x0000000000000000000000000000000000001235"
REDEEM_1 = "0x0000000000000000000000000000000000001236"
OWNER = "0x0000000000000000000000000000000000000abc"
# Deposit batchers take TOKEN (wraps USDC 1:1); redeem batchers take SHARE_TOKEN (wraps an ERC-4626 share of USDC).
TOKEN = "0x0000000000000000000000000000000000000c0c"
USDC = "0x0000000000000000000000000000000000000dc0"
SHARE_TOKEN = "0x0000000000000000000000000000000000000c5a"
SHARE = "0x0000000000000000000000000000000000000da5"
# USDC per whole share, in USDC units; and DefiLlama's USD price of USDC.
USDC_PER_SHARE = 1_050_000
USDC_PRICE = 2.0
TOKENS = {
    TOKEN: {"underlying()": (["address"], USDC), "rate()": (["uint256"], 1)},
    SHARE_TOKEN: {"underlying()": (["address"], SHARE), "rate()": (["uint256"], 10**12)},
    USDC: {"symbol()": (["string"], "USDC"), "decimals()": (["uint8"], 6)},
    SHARE: {"symbol()": (["string"], "vUSDC"), "decimals()": (["uint8"], 18), "asset()": (["address"], USDC),
            "convertToAssets(uint256)": (["uint256"], USDC_PER_SHARE)},
}
LATEST_BLOCK = 1_000_000
# Unwrap request ids, and the cleartext each finalized unwrap logged on TOKEN.
FINALIZED_UNWRAP = "0x" + "aa" * 32
INFLIGHT_UNWRAP = "0x" + "bb" * 32
UNWRAPPED = {FINALIZED_UNWRAP: 12_500_000}
ALICE, BOB, CAROL = (f"0x{n:040x}" for n in (0xA11CE, 0xB0B, 0xCA401))
# DEPOSIT's batch logs in chain order: (event, batch id, account).
# Batch 4: Bob quits before dispatch (leaves), Carol after it (a refund; still a depositor).
DEPOSIT_LOGS = [
    ("Joined", 4, ALICE),
    ("Joined", 4, BOB),
    ("Joined", 4, CAROL),
    ("Quit", 4, BOB),
    ("BatchDispatched", 4, None),
    ("Quit", 4, CAROL),
    ("Joined", 5, ALICE),
    ("Joined", 5, ALICE),
]


def selector(signature: str) -> str:
    return function_signature_to_4byte_selector(signature).hex()


# Per batcher: own fields, and per batch id (state, createdAt, dispatchedAt, minAge, callbackDeadline, unwrapRequestId).
CHAIN = {
    DEPOSIT: {
        "paused": False,
        "fromToken": TOKEN,
        "currentBatchId": 5,
        "minBatchAge": 86280,
        "batches": {5: (0, NOW - 2000, 0, 1800, 43200, 0), 4: (2, NOW - 5000, NOW - 1000, 1800, 43200, FINALIZED_UNWRAP)},
    },
    REDEEM_0: {
        "paused": False,
        "fromToken": SHARE_TOKEN,
        "currentBatchId": 3,
        "minBatchAge": 1800,
        "batches": {3: (0, NOW - 4000, 0, 1800, 43200, 0), 2: (1, NOW - 9000, NOW - 4000, 1800, 43200, INFLIGHT_UNWRAP)},
    },
    REDEEM_1: {
        "paused": True,
        "fromToken": SHARE_TOKEN,
        "currentBatchId": 1,
        "minBatchAge": 1,
        "batches": {1: (0, NOW - 100, 0, 1, 43200, 0)},
    },
}


def answer(to: str, data: str) -> str | None:
    """The ABI-encoded return of `data` on the stub batcher or token `to`; None for a revert."""
    sel, args = data[2:10], bytes.fromhex(data[10:])
    if to.lower() in TOKENS:
        for signature, (types, value) in TOKENS[to.lower()].items():
            if sel == selector(signature):
                return "0x" + encode(types, [value]).hex()
        return None
    batcher = CHAIN[to]
    own = {
        selector("paused()"): (["bool"], batcher["paused"]),
        selector("owner()"): (["address"], OWNER),
        selector("minBatchAge()"): (["uint256"], batcher["minBatchAge"]),
        selector("currentBatchId()"): (["uint256"], batcher["currentBatchId"]),
        selector("fromToken()"): (["address"], batcher["fromToken"]),
    }
    if sel in own:
        types, value = own[sel]
        return "0x" + encode(types, [value]).hex()
    batch_id = decode(["uint256"], args)[0]
    names = ["batchState", "batchCreatedAt", "batchDispatchedAt", "batchMinBatchAge", "batchCallbackDeadline", "unwrapRequestId"]
    fields = dict(zip(names, batcher["batches"][batch_id], strict=True))
    for name, value in fields.items():
        if sel == selector(f"{name}(uint256)"):
            return "0x" + (value[2:] if isinstance(value, str) else encode(["uint256"], [value]).hex())
    raise AssertionError(f"unexpected call {data}")


class StubResponse:
    def __init__(self, body, status: int = 200):
        self.body, self.status_code, self.headers, self.text = body, status, {}, json.dumps(body)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        return self.body

    def raise_for_status(self) -> None:
        if not self.ok:
            raise bc.requests.HTTPError(str(self.status_code))


def batcher_logs(query: dict) -> list[dict]:
    """DEPOSIT's Joined/Quit/BatchDispatched logs matching `query`'s batch ids."""
    topic = {"Joined": bc.JOINED, "Quit": bc.QUIT, "BatchDispatched": bc.BATCH_DISPATCHED}
    ids = query["topics"][1]
    out = []
    for index, (event, batch_id, account) in enumerate(DEPOSIT_LOGS):
        batch_topic = "0x" + encode(["uint256"], [batch_id]).hex()
        if batch_topic in ids:
            topics = [topic[event], batch_topic] + (["0x" + encode(["address"], [account]).hex()] if account else [])
            out.append({"topics": topics, "data": "0x", "blockNumber": hex(LATEST_BLOCK), "logIndex": hex(index)})
    return out


def unwrap_logs(query: dict) -> list[dict]:
    """The UnwrapFinalized logs of TOKEN matching `query`'s request ids, or DEPOSIT's batch logs."""
    if query["address"].lower() == DEPOSIT:
        return batcher_logs(query)
    if query["address"].lower() != TOKEN:
        return []
    ids = query["topics"][2]
    return [
        {"topics": [bc.UNWRAP_FINALIZED, "0x" + "00" * 32, rid], "data": "0x" + encode(["bytes32", "uint64"], [b"\0" * 32, value]).hex()}
        for rid, value in UNWRAPPED.items()
        if query["address"].lower() == TOKEN and rid in ids
    ]


def stub_prices(url: str, timeout: float, headers: dict) -> StubResponse:
    """DefiLlama's current-price answer: USDC only."""
    coins = url.rsplit("/", 1)[1].split(",")
    return StubResponse({"coins": {c: {"price": USDC_PRICE, "symbol": "USDC"} for c in coins if c == f"ethereum:{USDC}"}})


class StubSession:
    """A JSON-RPC node over CHAIN; `chain_id` is what eth_chainId answers, logs before `pruned_before` are refused.

    The first `rate_limited[method]` calls of a method answer QuickNode's per-call rate-limit error.
    """

    def __init__(self, chain_id: int = 1):
        self.headers: dict[str, str] = {}
        self.chain_id = chain_id
        self.pruned_before = 0
        self.rate_limited: dict[str, int] = {}

    def post(self, _url, json, timeout):  # noqa: A002 - mirrors requests' keyword
        out = []
        for call in json:
            if self.rate_limited.get(call["method"], 0) > 0:
                self.rate_limited[call["method"]] -= 1
                out.append({"id": call["id"], "error": {"code": -32007, "message": "500/second request limit reached"}})
            elif call["method"] == "eth_chainId":
                out.append({"id": call["id"], "result": hex(self.chain_id)})
            elif call["method"] == "eth_getBlockByNumber":
                out.append({"id": call["id"], "result": {"number": hex(LATEST_BLOCK), "timestamp": hex(NOW)}})
            elif call["method"] == "eth_getLogs":
                query = call["params"][0]
                if int(query["fromBlock"], 16) < self.pruned_before:
                    out.append({"id": call["id"], "error": {"code": -32602, "message": "Archive requests require a personal token"}})
                else:
                    out.append({"id": call["id"], "result": unwrap_logs(query)})
            else:
                params = call["params"][0]
                result = answer(params["to"], params["data"])
                if result is None:
                    out.append({"id": call["id"], "error": {"code": 3, "message": "execution reverted"}})
                else:
                    out.append({"id": call["id"], "result": result})
        return StubResponse(out)


@pytest.fixture
def book(tmp_path: Path) -> Path:
    path = tmp_path / "mainnet.json"
    path.write_text(json.dumps({
        "chainId": 1,
        "vaults": {"curator-usdc": {"depositBatcher": DEPOSIT, "redeemBatchers": [REDEEM_0, REDEEM_1]}},
    }))
    return path


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> StubSession:
    session = StubSession()
    original = bc.Rpc.__init__
    monkeypatch.setattr(bc.Rpc, "__init__", lambda self, url, session_=None: original(self, url, session))
    monkeypatch.setattr(bc.time, "time", lambda: NOW)
    monkeypatch.setattr(bc.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(bc.requests, "get", stub_prices)
    monkeypatch.setenv("BATCHERS_CLI_CONFIG", "/nonexistent")
    return session


def schedule(book: Path, *args: str) -> list[dict]:
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "schedule", "--json", *args])
    return json.loads(result.output)


def by_side(records: list[dict]) -> dict[str, dict]:
    return {r["side"]: r for r in records}


@pytest.mark.parametrize(
    ("side", "waiting"),
    [("deposit", "due"), ("redeem-0", "previous-batch"), ("redeem-1", "paused")],
)
def test_schedule_names_what_each_batcher_waits_on(book, chain, side, waiting):
    assert by_side(schedule(book))[side]["waiting"] == waiting


def test_next_dispatch_is_creation_plus_pinned_age(book, chain):
    assert by_side(schedule(book))["deposit"]["nextDispatchAt"] == bc.iso(NOW - 2000 + 1800)


def test_previous_batch_state_is_named(book, chain):
    assert by_side(schedule(book))["redeem-0"]["previousState"] == "Dispatched"


def test_genesis_batch_has_no_previous(book, chain):
    assert by_side(schedule(book))["redeem-1"]["previousBatchId"] is None


def test_side_filter_keeps_redeem_batchers(book, chain):
    assert [r["side"] for r in schedule(book, "--side", "redeem")] == ["redeem-0", "redeem-1"]


def test_waiting_filter_is_client_side(book, chain):
    assert [r["side"] for r in schedule(book, "--waiting", "paused")] == ["redeem-1"]


def test_limit_keeps_the_soonest(book, chain):
    assert [r["side"] for r in schedule(book, "--limit", "1")] == ["redeem-0"]


def test_unknown_vault_names_the_fix(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "schedule", "--vault", "nope"])
    assert "batchers vaults" in result.output


def test_wrong_chain_rpc_is_refused(book, chain):
    chain.chain_id = 11155111
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "schedule"])
    assert "fix --rpc-url" in result.output


def test_batcher_by_slug_shows_history(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "batcher", "curator-usdc/redeem-0", "--history", "2", "--json"])
    assert [b["state"] for b in json.loads(result.output)[0]["batches"]] == ["Pending", "Dispatched"]


def test_unknown_side_lists_the_vault_sides(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "batcher", "curator-usdc/redeem-9"])
    assert "deposit, redeem-0, redeem-1" in result.output


def test_vaults_reads_no_chain(book, monkeypatch):
    monkeypatch.setenv("BATCHERS_CLI_CONFIG", "/nonexistent")
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "vaults", "--json"])
    assert [r["side"] for r in json.loads(result.output)] == ["deposit", "redeem-0", "redeem-1"]


def test_vaults_pretty_prints_full_addresses(book, monkeypatch):
    monkeypatch.setenv("BATCHERS_CLI_CONFIG", "/nonexistent")
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "vaults", "--pretty"])
    assert REDEEM_1 in result.output


def test_missing_address_book_names_the_config(monkeypatch):
    monkeypatch.setenv("BATCHERS_CLI_CONFIG", "/nonexistent")
    monkeypatch.delenv("BATCHERS_ADDRESS_BOOK", raising=False)
    result = CliRunner().invoke(bc.cli, ["vaults"])
    assert "--address-book" in result.output


def test_schedule_pretty_titles_each_vault(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "schedule", "--pretty"])
    assert "curator-usdc" in result.output


def test_batcher_pretty_lists_its_batches(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "batcher", "curator-usdc/redeem-0", "--history", "2", "--pretty"])
    assert "Dispatched" in result.output


def test_schedule_reports_previous_batch_unshielded(book, chain):
    assert by_side(schedule(book))["deposit"]["previousUnshielded"] == "12.5"


def test_unwrap_not_finalized_has_no_unshielded(book, chain):
    assert by_side(schedule(book))["redeem-0"]["previousUnshielded"] is None


def test_pruned_logs_leave_unshielded_unknown(book, chain):
    chain.pruned_before = LATEST_BLOCK
    assert by_side(schedule(book))["deposit"]["previousUnshielded"] == "?"


def test_rate_limited_logs_are_retried_not_unknown(book, chain):
    chain.rate_limited = {"eth_getLogs": 1}
    assert by_side(schedule(book))["deposit"]["previousUnshielded"] == "12.5"


def test_rate_limited_calls_are_retried(book, chain):
    chain.rate_limited = {"eth_call": 5}
    assert by_side(schedule(book))["deposit"]["waiting"] == "due"


def test_persistent_rate_limit_is_an_error(book, chain):
    chain.rate_limited = {"eth_getLogs": 1000}
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "schedule"])
    assert "rate-limited" in result.output


def test_quit_before_dispatch_leaves_the_batch(book, chain):
    assert by_side(schedule(book))["deposit"]["previousDepositors"] == 2


def test_open_batch_counts_each_account_once(book, chain):
    assert by_side(schedule(book))["deposit"]["depositors"] == 1


def test_batch_without_joins_has_no_depositors(book, chain):
    assert by_side(schedule(book))["redeem-0"]["depositors"] == 0


def test_pruned_logs_leave_depositors_unknown(book, chain):
    chain.pruned_before = LATEST_BLOCK
    assert by_side(schedule(book))["deposit"]["previousDepositors"] == "?"


def test_batcher_history_reports_each_batch_depositors(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "batcher", "curator-usdc/deposit", "--history", "2", "--json"])
    assert [b["depositors"] for b in json.loads(result.output)[0]["batches"]] == [1, 2]


def test_batcher_history_reports_each_batch_unshielded(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "batcher", "curator-usdc/deposit", "--history", "2", "--json"])
    assert [b["unshielded"] for b in json.loads(result.output)[0]["batches"]] == [None, "12.5"]


def test_batcher_pretty_shows_unshielded_column(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "batcher", "curator-usdc/deposit", "--history", "2", "--pretty"])
    assert "$25.00" in result.output


@pytest.mark.parametrize(
    ("raw", "token", "text"),
    [
        (12_500_000, {"decimals": 6, "rate": 1}, "12.5"),
        (50_000_000_000, {"decimals": 6, "rate": 1}, "50000"),
        (507_777, {"decimals": 8, "rate": 100}, "0.507777"),
        (1_500_000, {"decimals": 18, "rate": 10**12}, "1.5"),
        (0, {"decimals": 6, "rate": 1}, "0"),
        (7, None, "7"),
    ],
)
def test_amount_in_whole_underlying_tokens(raw, token, text):
    assert bc.amount(raw, token) == text


@pytest.mark.parametrize(
    ("whole", "text"),
    [("1602071.312643", "1,602,071.31 USDT"), ("0.507777", "0.507777 USDT"), ("0", "0 USDT"), ("?", "?"), (None, "-")],
)
def test_units_formats_with_separators_and_symbol(whole, text):
    assert bc.units(whole, {"symbol": "USDT"}) == text


@pytest.mark.parametrize(("value", "text"), [(1600382.171, "$1,600,382.17"), (0.0, "$0.00"), (None, "-")])
def test_dollars(value, text):
    assert bc.dollars(value) == text


def test_previous_unshielded_usd_is_amount_times_price(book, chain):
    assert by_side(schedule(book))["deposit"]["previousUnshieldedUsd"] == 25.0


def test_unshielded_token_is_the_underlying(book, chain):
    assert by_side(schedule(book))["deposit"]["unshieldedToken"]["symbol"] == "USDC"


def test_erc4626_share_priced_through_its_asset(book, chain):
    assert by_side(schedule(book))["redeem-0"]["unshieldedToken"]["priceUsd"] == pytest.approx(1.05 * USDC_PRICE)


def test_text_line_shows_units_and_dollars(book, chain):
    result = CliRunner().invoke(bc.cli, ["--address-book", str(book), "schedule", "--vault", "curator-usdc", "--side", "deposit"])
    assert "unshielded 12.50 USDC ($25.00)" in result.output


def test_prices_unavailable_leave_usd_empty(book, chain, monkeypatch):
    def down(*_args, **_kwargs):
        raise bc.requests.ConnectionError

    monkeypatch.setattr(bc.requests, "get", down)
    assert by_side(schedule(book))["deposit"]["previousUnshieldedUsd"] is None


@pytest.mark.parametrize(("side", "order"), [("deposit", (0, 0)), ("redeem-10", (1, 10))])
def test_side_order_puts_deposit_first(side, order):
    assert bc.side_order(side) == order


@pytest.mark.parametrize(("seconds", "text"), [(45, "45s"), (1800, "30m"), (86280, "23h58m"), (518400, "6d00h")])
def test_duration(seconds, text):
    assert bc.duration(seconds) == text


@pytest.mark.parametrize(("seconds", "text"), [(240, "in 4m"), (-13, "13s ago")])
def test_relative(seconds, text):
    assert bc.relative(seconds) == text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
