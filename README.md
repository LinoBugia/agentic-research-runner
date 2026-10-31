# agentic research runner (`arr`)

> 🚧 **Under construction.** `arr` is still being built. The agent core works, the user interface is in progress. Interfaces, commands and file formats can change at any time. The project is not ready for production use.

AI agents now prove real mathematics. In [openai/math](https://github.com/openai/math), OpenAI has published 721 manuscripts on open research problems (as of October 2026). About 42% of the top-line results are formalized in Lean, and OpenAI notes that unformalized results could still have issues. For its [Navier–Stokes solution](https://openai.com/index/navier-stokes-solution/), OpenAI reports using a system of coordinated agents: roughly 10,000 concurrently active agents worked in one group, communicated with each other and ran code, under monitoring and isolation. Both came from unreleased internal models.

`arr` rebuilds this setup on a small scale: a handful of agents instead of thousands, models you can use yourself, and your own machine. Here too, agents work in groups, talk to each other in chats, run code and stay inside a sandbox. The details of OpenAI's system are not public, and `arr` is not an OpenAI project.

## The idea

`arr` is a local workbench for mathematical research with AI agents. You put together a **research group** of Claude and ChatGPT agents and give it a task. Each agent gets a role and its own working folder. The agents compute, write Python, talk in group chats and rate each other's results. You keep the overview and stay in control.

A single model quickly agrees with itself on a proof. A group with clear roles can do more: one agent drafts the strategy, a second carries out the proof, a third checks it critically. If the result falls short, the work goes back for another round. Models from different vendors work in the same group.

Setups like [OpenProver](https://github.com/Kripner/openprover), where a planner coordinates the proof search and workers take on subtasks, can be mapped to the roles of a research group. `arr` has no formal verification in Lean yet.

## What `arr` does

- **Mixed groups:** Claude and Codex agents run in the same run, each with its own role and working folder.
- **Group chats:** Agents talk in channels, ask each other direct questions and attach files. Questions reach an agent even in the middle of its work.
- **Workflows:** Solo work, round-table discussions, or a waterfall with ratings and rework.
- **Full record:** Every run keeps track of who did and wrote what, and when: transcripts, chats, results, changes and costs.
- **Safety:** Agents run in a sandbox and only read and write what they are allowed to. `arr` asks for your approval before risky steps such as installations. After every run, `arr` scans the run folder for secrets.
- **Local:** The service runs on your machine. Credentials stay in the macOS keychain or in your existing logins.

## Status

| Wave | Topic | Status |
|---|---|---|
| 0 | Foundations and technical groundwork | ✅ done |
| 1 | Core library and clickable prototype | ✅ done |
| 2 | Real agents: mixed runs from the CLI | ✅ done |
| 3 | Service and UI with real data | 🚧 in progress |
| 4 | Starting runs from Claude Code, ready-made group skills | planned |
| 5 | Polish, onboarding, robustness | planned |

The order of the work packages is shown in the [plan diagram](umsetzung/Codeplan_Ablauf.md) (German).

The user interface and CLI messages are in German for now.

## Try it

`arr` is developed on macOS with Python 3.12 and [uv](https://docs.astral.sh/uv/). Instructions for real runs will follow once setup through the UI is finished. Real runs need your own Claude or ChatGPT access.

```bash
uv sync                                         # set up the environment
uv run arr --help                               # list commands
uv run arr info                                 # version, Python, platform
uv run pytest                                   # tests (no real models)
uv run ruff check && uv run ruff format --check # linter and format
```

## License

[MIT](LICENSE)
