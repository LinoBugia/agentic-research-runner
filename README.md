# agentic research runner (`arr`)

> 🚧 **Under construction.** The agent core works and the user interface is on its way. Interfaces, commands and file formats may change at any time, so `arr` isn't ready for production use yet.

AI agents are now proving real mathematics. OpenAI has published 721 manuscripts on open research problems in [openai/math](https://github.com/openai/math) (as of October 2026). About 42% of the main results are formalized in Lean, and OpenAI cautions that some of the others may still have issues. For its [Navier–Stokes solution](https://openai.com/index/navier-stokes-solution/), OpenAI reports running a system of coordinated agents: a group of roughly 10,000 concurrently active agents that talked to each other and ran code, all monitored and isolated. Both efforts relied on unreleased internal models.

`arr` brings that setup down to a small scale: a handful of agents instead of thousands, using models you have access to, on your own machine. Agents still work in groups, talk to each other in chats and run code, all inside a sandbox.

## The idea

`arr` is a local workbench for doing mathematical research with AI agents. You assemble a **research group** of Claude and ChatGPT agents and hand it a problem. Each agent has its own role and working folder. They compute, write Python, discuss in group chats and rate each other's results, while you keep an eye on everything and stay in control.

A single model is quick to agree with itself about a proof. A group with clear roles does better: one agent drafts the strategy, another writes the proof, and a third tries to break it. If the result doesn't hold up, the work goes back for another round. Models from different vendors can work side by side in the same group.

Setups like [OpenProver](https://github.com/Kripner/openprover), where a planner coordinates the proof search and hands subtasks to workers, map naturally onto the roles of a research group. `arr` doesn't do formal verification in Lean yet.

`arr` will also be available as an MCP server, so you can use it right from an agent like Claude. You keep working with your mathematical companion the way you always have, just with more behind it: your agent can spawn groups of workers to attack a problem, send them off to explore different ideas, check on their progress and read the results. This part is planned for wave 4.

## What `arr` does

- **Mixed groups:** Claude and Codex agents work together in one run, each with its own role and working folder.
- **Group chats:** Agents talk in channels, ask each other direct questions and share files. A question reaches an agent even in the middle of its work.
- **Workflows:** Solo work, round-table discussions, or a waterfall with ratings and rework rounds.
- **Full record:** Every run records who did what and when: transcripts, chats, results, file changes and costs.
- **Safety:** Agents run in a sandbox and can only read and write what they're allowed to. Risky steps such as installing packages need your approval, and every run ends with a scan for leaked secrets.
- **Local:** Everything runs on your machine. Credentials stay in the macOS keychain or your existing logins.

## Security

Agents in `arr` work on very hard, sometimes unsolvable problems, for hours and without confirming every command. Under that kind of pressure, models look for ways around their limits. `arr` therefore assumes that every agent actively tries to escape its sandbox, and only counts a limit as real when a test proves it.

[SECURITY.md](SECURITY.md) describes the threat model, the principles and layers, how they are implemented on macOS, and which ways out are known, closed or still open. Until agents run under a separate macOS user, real runs should stay supervised.

## Status

| Wave | Topic | Status |
|---|---|---|
| 0 | Foundations and technical groundwork | ✅ done |
| 1 | Core library and clickable prototype | ✅ done |
| 2 | Real agents: mixed runs from the CLI | ✅ done |
| 3 | Service and UI with real data | 🚧 in progress |
| 4 | MCP server for Claude Code, ready-made group skills | planned |
| 5 | Polish, onboarding, robustness | planned |

The [plan diagram](umsetzung/Codeplan_Ablauf.md) (in German) shows how the work packages fit together. The user interface and CLI messages are also in German for now.

## Try it

`arr` is developed on macOS with Python 3.12 and [uv](https://docs.astral.sh/uv/). Instructions for real runs will follow once setup through the UI is finished. You'll need your own Claude or ChatGPT access for those.

```bash
uv sync                                         # set up the environment
uv run arr --help                               # list commands
uv run arr info                                 # version, Python, platform
uv run pytest                                   # tests (no real models)
uv run ruff check && uv run ruff format --check # linter and format
```

## License

[MIT](LICENSE)
