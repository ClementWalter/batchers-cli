---
name: batchers-cli
description: >-
  Read when each confidential vault batcher dispatches, from chain, with the
  bundled `batchers` command. For every vault in an address book it lists the
  deposit batcher and each redeem batcher of the rotation: current batch, min
  batch age (policy and pinned), next dispatch time in UTC, the previous
  batch's state, the owner, and what the batch waits on (paused, previous
  batch in flight, age, or due). `batcher` shows one batcher with its last
  batches. Read-only. Every read supports --json. Triggers - "batcher status",
  "when does this vault dispatch", "next dispatch", "redeem batchers
  schedule", "is a batch in flight", "which batchers are paused", "warm-up
  batchers", "batchers cli".
allowed-tools:
  - Bash
  - Read
---

# Batchers CLI

Reads vault batchers (OZ `BatcherConfidential`) over JSON-RPC, using an address
book that names each vault's batchers.

## How to invoke

Invoke it as **`batchers`**, on `$PATH` through `claudine/scripts/link_clis.py`,
which links this repo's `bin/batchers` into `~/.local/bin`, so a `git pull` or an
uncommitted edit takes effect at once. It is a PEP 723 `uv` script;
dependencies resolve on first run. If `batchers` is missing, run
`<skill-dir>/bin/batchers`.

## Authentication

None for the chain: the default RPC is a public node (publicnode.com). The
address book usually lives in a private repo, read through `gh` with the
user's GitHub login. Configure it once in `~/.config/batchers-cli/config.json`
(chmod 600):

```json
{
  "address_book": "gh:<owner>/<repo>",
  "rpc": {"mainnet": "<url or op://vault/item/field>"},
  "labels": {"0x…": "dao", "0x…": "deployer"}
}
```

- `address_book`: a checkout dir (reads `contracts/deployments/<network>.json`),
  a `<network>.json` file, or `gh:<owner>/<repo>[@ref]`. Overridden by
  `--address-book` and `$BATCHERS_ADDRESS_BOOK`.
- `rpc.<network>`: overridden by `--rpc-url` and `$BATCHERS_RPC_URL`.
- `labels`: owner names printed instead of addresses.
- `$BATCHERS_CLI_CONFIG` points at another config file.

## Output conventions

- Default: one line per batcher, the full batcher address tab-separated last.
  Times are UTC, durations like `23h58m`.
- `--json`: normalized records: ISO UTC times, seconds, batch state names,
  `waiting` as `paused | previous-batch | age | due`.
- `--raw` (`schedule` only): the decoded getter results per batcher.
- Logs and RPC retries go to stderr; `--debug` for more.

## Commands

```bash
# Read
batchers vaults                                   # vault, side, address; no chain read
batchers schedule                                 # every batcher, soonest dispatch first
batchers schedule --vault curator-usdc --vault curator-usdt
batchers schedule --side redeem --waiting previous-batch
batchers schedule --limit 5 --json
batchers batcher curator-usdc/redeem-2               # REF: <vault>/<side> or an address
batchers batcher curator-usdt/deposit curator-usdt/redeem-0 --history 5 --json
batchers --network sepolia schedule
```

`side` is `deposit` or `redeem-<i>`, the index in the vault's `redeemBatchers`.

## Recipes

- **Is any redeem batch still in flight?** (before a launch, or when a slot is
  late) `batchers schedule --side redeem --waiting previous-batch`
- **What dispatches in the next hour?**
  `batchers schedule --json | jq -r '.[] | select(.waiting == "age") | [.nextDispatchAt, .vault, .side] | @tsv' | head`
- **Which batchers has the launch not handed over yet?** Label the deployer in
  `labels`, then `batchers schedule | grep 'owner deployer'`.
- **A batcher's recent history:** `batchers batcher <vault>/<side> --history 10`.

## Gotchas

- A batch dispatches at `batchCreatedAt + batchMinBatchAge`. The age is pinned
  when the batch opens, so a `setMinBatchAge` moves only the next batch: compare
  `age` (pinned) with `policy`.
- A batcher keeps one batch in flight. While its previous batch is `Dispatched`
  (an async partner has not paid out yet), the current batch waits even past
  its time: `waiting previous-batch`.
- Batch 1 is the genesis batch and has no previous one. An empty batch the
  keeper dispatched is `Canceled`, which is normal.
- `callback deadline` in `batcher` is a duration after dispatch, not a time.
- Measured 2026-11-17: a full mainnet `schedule` (48 batchers, about 720
  `eth_call`s in batches of 40) hits publicnode's 429 a few times and finishes
  in about 20 s through the retries. Set `rpc.<network>` to a keyed node for
  speed.
- The RPC must serve the address book's `chainId`; a mismatch is an error.
