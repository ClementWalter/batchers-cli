# batchers-cli

**Visibility: public.**

**Extension: not shipped** (it reads an address book from a private repo through
the developer's own `gh` login; there is nothing a person without a terminal
could set up).

Read when each confidential vault batcher dispatches, from chain. For every
vault in an address book, `batchers` lists the deposit batcher and each redeem
batcher: current batch, min batch age, next dispatch time, the previous batch's
state, the owner, and what the batch waits on. Read-only.

## Install

`batchers` is a [PEP 723](https://peps.python.org/pep-0723/) `uv` script, so its
dependencies resolve on first run:

```bash
git clone https://github.com/ClementWalter/batchers-cli.git
ln -sfn "$PWD/batchers-cli/bin/batchers" ~/.local/bin/batchers
```

Requires [uv](https://docs.astral.sh/uv/), and the GitHub CLI `gh` when the
address book is read from GitHub.

## Configure

`~/.config/batchers-cli/config.json`:

```json
{
  "address_book": "gh:<owner>/<repo>",
  "labels": {"0x…": "dao"}
}
```

`address_book` also takes a checkout directory or a `<network>.json` file. The
RPC defaults to a public node; set `"rpc": {"mainnet": "<url>"}` for another.

## Use

```bash
batchers schedule
batchers schedule --vault <slug> --side redeem
batchers batcher <slug>/redeem-0 --history 5
batchers schedule --json | jq .
```

## Test

```bash
uv run --script tests/test_batchers_cli.py
```
