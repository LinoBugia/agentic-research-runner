# Security

> Status: 2026-11-27 · Research prototype · Implemented and tested on macOS only · Details: [docs/security.md](docs/security.md)

`arr` is a research prototype, not a hardened product. It runs AI agents on your own machine, with your own model subscriptions, and lets them execute code they write. Use it the way you would use any tool that runs model-generated code: on a machine and an account where a mistake is acceptable, with real runs supervised, and not as your only line of defence.

## Why observability comes first

`arr` is meant for agents that work for hours without a human confirming every command, on mathematical problems that may not be solvable with the means at hand. Long autonomy and hard or unsolvable goals are the setting in which agents have been seen to work around their limits: gaming a check, changing their environment, or reaching for things outside their task. This has happened in practice, not only in theory (see [docs/security.md §1](docs/security.md#1-the-conflict)). Released models do it rarely. We still do not want to depend on that.

So `arr` assumes little about what an agent will do and puts its weight on seeing what it did:

- Every agent's transcript, tool calls and submissions are kept with the run.
- Every permission request is logged with its reason, its decision and who decided.
- Usage is counted per login profile, request by request, and every run has limits for time, turns and budget.
- A kill switch per login profile stops every run that uses it.
- A secret scan runs at the end of every run.
- "Not solved" is a valid result. An agent that is blocked is told to stop and ask, not to try another way.

## Architecture in brief

- **One machine, local only.** A local service with a web GUI, reachable only under a random local host name and with a token. Claude Code talks to it through `arr mcp`.
- **Runs.** A run is a group of agents (Claude Code CLI and Codex) working in phases. Each agent has its own work, temp and scratch folders inside the run; a group can also give named agents a shared folder on purpose.
- **Sandbox.** Every shell command of an agent runs in the OS sandbox of its CLI (Seatbelt on macOS). It can write only inside the run, read only from an allowlist, and has no network unless a request is approved.
- **Exchange.** Agents exchange work only through a chat server and submissions. `arr` checks what they hand in outside the sandbox and treats it as untrusted data.
- **Logins.** Credentials stay in the macOS keychain or the providers' own logins. A small start program outside the agents' reach passes them to the model client. They are never placed in the agent's environment or files.

## Safety layers

| Layer | What it does | Status |
|---|---|---|
| 0 Purpose | Prompts state the limits; "not solved" is a valid result | in place |
| 1 OS sandbox | Write only inside the run, read from an allowlist, no network | in place (macOS) |
| 2 Fixed rules | Hard denies that no approval can lift (keychain, `sudo`, protected paths, other agents' folders, this machine on the network) | in place |
| 3 Approval request | The grey zone (network, package installs, reading outside the run) goes to an inbox with a reason | in place |
| 4 Decider | The user, or a supervising agent within limits the user set | in place |
| 5 Outside hygiene | Processes outside the sandbox never load code or settings from places an agent can write | in place |
| 6 Watch and stop | Logs, usage per profile, kill switch, secret scan | in place |
| 7 Separate OS user | Agents run under an account of their own, without the user's logins and files | **planned**, measured in a spike, not built |

A limit in layers 1, 2 and 5 counts only when a test proves it with a counter-test, that is, a test that fails when the protection is removed.

## Known limits

- Only macOS is implemented. Other platforms are a design template, nothing more.
- Without layer 7, agents run under your own account, inside the sandbox. On macOS a process can read the start environment of other processes of the same user. `arr` keeps its own secrets out of every environment, but it cannot protect secrets that other applications put there.
- Background processes an agent starts can outlive its run.
- Five known ways out are still open and three are accepted. One of them lies outside the sandbox: nothing in `arr` stops your own agent (Claude Code) from running a script that a research agent handed in. The full list, with what is closed and how it was tested, is in [docs/security.md §5.2](docs/security.md#52-known-ways-out).
- Closing the ways we know of proves nothing about the ones we do not. Every review so far has found new ones.

**Therefore:** real runs stay supervised. Hard problems run unsupervised only once layer 7 exists.

Section and principle numbers cited in code comments (for example "SECURITY.md §5.1") refer to [docs/security.md](docs/security.md).

## Reporting

Please report security findings to the maintainer directly, not in a public issue.
