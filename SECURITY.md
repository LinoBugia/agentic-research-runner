# Security model

> Status: 2026-11-10 · Platform implemented: macOS · Maintained with every change to sandbox, permissions or logins, every security review and every update of macOS, Claude Code CLI or Codex.

This document describes how `arr` keeps research agents from harming the machine they run on, which ways out we know of, and which of them are closed. It is written so that the general part can serve as a template for other operating systems and for servers. The platform part currently covers macOS only.

## 1. The conflict

`arr` runs groups of AI agents on hard mathematical problems. Many of these problems are open, some may not be solvable with the means at hand, and runs last for hours. To be useful, agents work without a human confirming every command: they write and run Python, create files and talk to each other.

A model that cannot reach its goal by legitimate means may look for other means: gaming a check, changing its environment or reaching for something outside its task. Released models are trained and tested against this, and in everyday use it is rare. It has been observed with models in development. In the OpenAI–Hugging Face incident of July 2026, agents ran on unreleased models with reduced safeguards, in training and evaluation runs where many tasks could not be solved legitimately. They left their sandbox through its one allowed network path and got into Hugging Face's infrastructure while looking for solutions. See [Wikipedia](https://en.wikipedia.org/wiki/OpenAI%E2%80%93HuggingFace_incident), [The Hacker News](https://thehackernews.com/2026/08/openai-says-reward-hacking-drove-ai.html) and [MIT Technology Review](https://www.technologyreview.com/2026/08/26/1143013/the-inside-story-on-why-openai-agents-hacked-hugging-face/).

`arr` uses released models and runs at a small scale. We still design for this case, for two reasons: hard research problems are the kind of long, demanding goal where it shows up, and without per-command confirmation the sandbox is the main barrier. Three goals follow:

1. **Agents have no reason to work around their limits.** "Not solved" is a valid result, and a blocked agent stops and asks.
2. **Limits are enforced, not requested.** The operating system and fixed rules enforce them, and tests prove them. We do not rely on goal 1.
3. **Runs are watched.** Approvals and decisions are logged, usage is counted per login, a kill switch stops runs, and real runs are supervised for now.

## 2. Threat model

**Attacker:** For design and tests we take the worst case. An agent in a run, or several working together, has shell access, file tools, time and the skills of a capable penetration tester. It tries to get around every limit, also over several steps and across turns, sessions and agents. This is a test assumption, not an expectation of how released models behave.

**Assets:**

- the user's logins: model subscriptions and API keys, the OS keychain, tokens held by other applications
- SSH keys, cloud credentials, browser data
- the user's files outside the run
- the network and other systems reachable from the machine
- `arr` itself: its settings, its start program, its permission rules
- budget and usage limits of the user's accounts

**Out of scope for now:** a malicious user, a compromised OS, attacks on the model providers' servers.

## 3. Principles

These hold on every platform.

1. **Test against the worst case.** A limit counts only if it is enforced by the operating system or by a fixed rule, and only if a test proves it with a counter-test (the test turns red when the protection is removed).
2. **Autonomy inside, approval at the edge.** Agents work freely inside the sandbox. Only the grey zone (network, package installation, reading outside the run) asks for approval. Forbidden actions are denied without asking. Because there is no per-command confirmation, the walls must hold on their own.
3. **Nothing outside the sandbox consumes what an agent can write.** No process outside the sandbox (agent CLI, app server, chat server, `arr` itself) executes code, loads modules or reads settings from a place an agent can write to, neither through the shell nor through file tools.
4. **File tools never get more than the shell.** Where tools such as Write or Edit run outside the OS sandbox, a fixed rule denies them every path the sandbox denies the shell, derived from the same source.
5. **An approval checks everything that follows it.** What an approval allows may run outside the sandbox. Codex, for example, applies an approved patch outside it. So the check covers every path, right and network target the action touches, not only the ones the request names first. Writing outside the run or onto protected paths is never approvable, not even by the user. A request the code cannot fully read is denied.
6. **Secrets never reach the agent.** No credential is placed in the agent's environment, files or logs. A small start program outside the agent's reach fetches it and hands it to the model client over a pipe.
7. **Fail closed, stop fast, count everything.** If a check cannot run, the run does not start. Every login profile has a kill switch that stops all runs using it. Usage is recorded per profile, request by request.
8. **Re-prove on change.** A version watcher forces the escape tests to run again after every update of the OS, the agent CLIs or their sandbox rules.
9. **Separate the user.** The last wall is an OS account of its own for the agents, without the user's logins and files. Until it exists, real runs are supervised.

## 4. Layers

| Layer | What it does | Decides |
|---|---|---|
| 0 Purpose | Prompts state the limits. "Not solved" and partial results are valid results; a blocked agent stops and says so or asks instead of retrying a denied step another way (the same text for both backends) | – |
| 1 OS sandbox | Every shell command: write only inside the run, read only from an allowlist, no network | kernel |
| 2 Fixed rules | Permission engine: hard denies (keychain tools, `sudo`, protected paths, `.git` via file tools, any file change outside the run, extra write roots, other agents' folders, this machine on the network) | code |
| 3 Approval request | Grey zone goes to an inbox with a one-line reason | – |
| 4 Decider | User, or a supervising agent within limits the user set | human / agent |
| 5 Outside hygiene | Processes outside the sandbox start isolated, with clean `PATH` and `HOME`, never from agent-writable places | code |
| 6 Watch and stop | Kill switch per profile, usage per profile, secret scan at the end of every run | user / code |
| 7 Separate OS user | Agents run as another account (planned) | kernel |

## 5. macOS

### 5.1 How it is solved

- **Sandbox:** Claude's Bash runs under Seatbelt via the Claude Code CLI sandbox. Codex runs under its own sandbox with an `arr` profile that denies the home folder. Both write only inside the run and read only from an allowlist (work folder, shared run folders, read folders, Python environment).
- **Logins:** Credentials live in the macOS keychain or in the providers' own logins. A start program in `~/.arr/bin` (outside every write root, run with `python -I`) reads the secret and passes it to the CLI through a file descriptor (`CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`). It never appears in the agent's environment.
- **Keychain lock probe:** Before a run, a probe checks inside the sandbox that the keychain cannot be read.
- **Outside processes:** The CLI's `PATH` contains no write root; the run's virtual environment appears only in the agent's own shell environment. The chat server starts with `python -I`. The Codex app server gets a `HOME` of its own, outside the run's temp folder. CLI, app server and chat server find a `git` stub first on their `PATH` that does nothing, so no `git` runs outside the sandbox; agents keep the real `git` in their shell. The CLI runs with Node's compile cache off.
- **File tools:** One deny list in the permission engine covers `.git`, `.gitattributes`, `.gitmodules`, `.claude`, `.codex`, `.agents`, `.mcp.json`, shell start files and similar names, checked case-insensitively before and after resolving links. Write, Edit, MultiEdit, NotebookEdit and Codex file changes on these paths are denied, and no approval lifts that. The same list goes to the Claude sandbox as `denyWrite`. Codex `apply_patch` cannot be switched off, so the Codex profile makes the top-level names read-only and a patch there becomes an approval request that is denied.
- **Codex approvals:** Every Codex file change is checked with all its paths, the target of a move included. `arr` denies, without asking anyone: any move (the agent can use `mv` in its shell instead), any request for an extra write root (`grantRoot`), any write outside the run or onto the deny list, any change of an unknown kind or with unknown fields, and any command request with an unknown field, a value of the wrong type or extra permissions (`additionalPermissions`). A request method `arr` does not know gets a JSON-RPC error. A fixed test holds the methods, fields and value types against the schema of the pinned Codex version (with its experimental fields) and turns red on another version. `arr` runs no tool on behalf of the app server. An approved command still runs inside the sandbox, on both backends.
- **Waiting on a marker file:** An agent may wait for a file in its own work folder (`until_file`, for long scripts). This is the one place where something outside the sandbox looks at an agent-chosen path. The room server and the orchestrator only check whether the file exists: relative path inside the work folder, no `..`, no absolute path, no link followed, no content read, nothing started. Tests cover link chains, races between check and use, hard links and FIFOs.
- **Other agents' folders:** Inside a run, every agent's own folders are closed to all other agents: work copy, transcript, events, raw data, blobs, plugin, its `shared/` work folder and its temp folder. Sealed reviews of other judges and the room database (`room.sqlite`, all channels) are closed as well. This holds for the shell on both backends and for Read, Grep and Glob, with no approval path, also under an "always" rule for the run folder or the work folder. Agents exchange work only through chat and results (`output/`). The one configured exception is a read folder `run:shared/<name>` set by the group's author; `check` rejects it where it would break the independence of judges or of isolated explorers. The run's Python environment stays shared on purpose; an agent can only use it as a drop on purpose.
- **Temp folder per agent:** Each agent gets `tmp/<id>/` as `TMPDIR`, as `CLAUDE_CODE_TMPDIR` (through a short alias `/tmp/arr-<hash>` of its own) and, for Codex, as its command `HOME`. Output of background commands lands there too.
- **This machine on the network:** Requests to `localhost`, `127.0.0.0/8`, `::1`, `0.0.0.0`, `*.localhost` and the service's random host name are denied by a fixed rule, without asking and also under an "always" rule. Wildcard hosts that include this machine (`*`, `*.*`, `*.localhost`, `127.*` …) never reach the sandbox's `allowedDomains`, and a rule containing one does not load. Measured: the Claude sandbox's proxy does not block loopback by itself, so this protection rests on `arr`'s rule alone. Names that resolve to `127.0.0.1` through DNS can still be approved; the `arr` service answers only under its random host name.
- **Claude Code bridge:** `arr mcp` is a stdio server that Claude Code starts in a project. It reads the address, host name and token of the running service from `service.json` (mode 0600) at run time only and never puts the token in its command line or environment. The service answers only under its random host name (check against DNS rebinding) and only with the token.
- **Run starts from Claude Code:** Every run that Claude Code starts waits for the user's approval in the GUI. The card lists every deviation that widens rights: web, read folders, work folder mode, decider, limits above the standard, other profiles, extra agents, terminal. No tool can approve a run start, decide an approval or escalation, or answer an agent's question to the user. A message from Claude Code wakes an agent but never answers its question, also when it addresses or replies to it. Claude Code cannot set the supervising agent's mandate or model.
- **Agent text to Claude Code:** Agent text that reaches Claude Code is marked as data and cleaned of control, invisible format and tag characters. Error messages never quote argument values.
- **Helper scripts the user's agent may run:** Start skills marked for both audiences can also be run by Claude Code, outside every sandbox, possibly on text taken from an agent's result. `sympy-check` therefore checks every expression against an allowlist (numbers, names, operators, brackets, known SymPy functions) before SymPy parses it. Nothing else reaches the parser, and the allowlist is the only wall. The script also has a CPU limit of 10 s and rejects oversized numbers before computing. Memory is not limited on macOS, which refuses the limit: an expansion can take a few GB for those 10 s.
- **Installing start skills:** "Add start skills", `arr init` and the project setup write only new files below `<workfolder>/skills/`, follow no link and leave nothing half written.
- **Supervising agent (layer 4):** With the decider `gatekeeper_then_user` or `gatekeeper`, a model decides simple requests within the user's mandate. A fixed check runs first and does not trust the model. It lets through only network requests to the mandate's hosts (in v1 at most `pypi.org`, `files.pythonhosted.org`, `arxiv.org`) and single, plain `pip install` commands of PyPI names (checked against an allowlist following PEP 508/503, no files, paths, URLs or other indexes). Everything else goes to the user or is denied without a call.
  - The model can only allow once. It never decides run starts, escalations, questions to the user or Codex commands with extra rights.
  - The call runs the Claude CLI without tools, MCP servers, plugins or settings from agent-writable places. It gets a cleaned environment with no inherited login, address or session variables and the same outside hygiene as the other outside processes. The login comes only from the profile's start program.
  - Every call counts in usage per profile and against the budget, with at most 50 per run.
  - The kill switch knows the supervising agent's profile: blocking it stops the run and cancels a running call, whose answer then does not count.
  - Withdrawing a decision takes effect at once, also for requests already being checked.
  - The prompt shows only the asking agent's own history.
- **Agent text in the GUI:** The GUI holds the service token, so script in the page could click approvals for the user. All agent and skill text is rendered with every `<` escaped before Markdown. The HTML is then rebuilt from an allowlist of tags and attributes with no event handlers or styles; links may only be `http`, `https`, `mailto` or relative, and images only relative. Images from other addresses are shown as text and never loaded. A Content-Security-Policy on every response (`img-src 'self' data:`, `media-src 'self'`, `object-src 'none'`, `frame-src 'none'`, `script-src-attr 'none'`) is a second wall for images and event handlers. It does not stop `javascript:` links or inline scripts (NiceGUI writes inline scripts without a nonce), so for those the allowlist is the only wall.
- **Codex project config:** `arr` records the state of the work folder's `.codex/` at run start and starts no further thread once it has changed.
- **Tests:** Escape tests run the real CLIs against a fake model server in a test `HOME` with random bait files, and check results by exit code only. A `security` stub keeps the real keychain out of every lab.

### 5.2 Known ways out

| # | Way | Status |
|---|---|---|
| 1 | Read logins, keys or files outside the run directly | closed, fixed tests with counter-test |
| 2 | Read the environment of other processes of the same user (`KERN_PROCARGS2`, see 5.3) | **open**, needs layer 7 |
| 3a | Plant code in a folder on the outside process's `PATH` or import path (run venv, `python -m` from the work folder) | closed (W3-06.3) |
| 3b | Plant git settings in an outside process's `HOME` | closed (W3-06.3) |
| 3c | Create `.git` with the Write tool, which runs outside the sandbox, so the CLI's own `git` runs an agent's filter | closed (W3-06.4) |
| 3d | Codex `apply_patch` writes `.git/config` and `.codex/config.toml`, which its shell sandbox protects; the app server runs `git` outside the sandbox every turn | closed (W3-06.4) |
| 3e | A `.codex/config.toml` written during a run adds an MCP server that the next app server starts outside the sandbox | measured: it does; closed (W3-06.4) |
| 3f | The CLI's Node writes its compile cache into the run's temp folder and loads it as code on the next start | closed (W3-06.4) |
| 3g | Codex `apply_patch` with `Move to` writes outside the run and onto the deny list; the approval request named only the source, so `arr` approved it and Codex applied it outside the sandbox | closed (W3-06.5) |
| 3h | Codex applies moves and patches inside the run without asking, also to protected names below the top level such as `sub/.git` or `sub/.codex` | not closable with the pinned Codex version; measured: no effect outside the sandbox, the same as `mv` in the shell |
| 3i | `arr` answered an approval method it does not know with an empty result instead of an error | closed (W4-00): an unknown method gets the JSON-RPC error `-32601`, a request for a token `-32000`; a fixed test holds the methods and fields against the schema of the pinned Codex version and turns red on another version |
| 4 | Processes an agent starts in the background (`&`, `nohup`, `disown`, or a process detached with its own session) keep running after the run ends and after the kill switch. They stay inside the sandbox but keep using CPU and writing to the run folder | **open**, measured in W3-07. Prompts allow only Claude's `run_in_background`, which the CLI ends with the run; Codex agents run long scripts in the foreground. A follow-up makes run end and kill switch end every process of the run; layer 7 closes it fully |
| 5 | Read other agents' work through the room database `room.sqlite` (all channels, sealed reviews) | closed (W4-03) |
| 6 | Read another agent's drafts, transcript or work copy with an approval or an "always" rule for the run folder, or through the run's shared temp folder | closed (W4-03.1, W4-03.2): fixed read rule, temp folder per agent; measured on both backends with counter-tests |
| 7 | Reach `arr`'s own service on this machine through an approved network host or a wildcard rule such as `*` | closed (W4-03.1, W4-03.2) |
| 8 | A judge learns through Codex `apply_patch` (permission error instead of "not found") whether a sealed review exists, not its content | accepted |
| 9 | A read rule written by hand into another agent's folder (`agents/<other>`, `tmp/<other>`) opens it to the Claude shell; Codex and the file tools hold | **open**, low: no approval creates such a rule, only a hand-written one; follow-up planned |
| 10 | Claude Code answers an agent's question to the user by messaging the agent, so the user never sees it | closed (W4-01.2, W4-03.1): such a message wakes the agent but answers nothing; tested through `arr mcp` with every form of address |
| 11 | Claude Code changes the decider or the supervising agent's mandate in a run start without the card showing it | closed (W4-01.1): the mandate cannot be set from Claude Code; another decider is always shown as a deviation that widens rights |
| 12 | An agent puts an expression into its result that runs as Python when the user's agent checks it with `sympy-check` outside the sandbox | closed (W4-05.1): allowlist before parsing; 99 attacks, none ran code |
| 13 | The supervising agent's call inherited a login or API address from the `arr` process, so it could run on another login, or send the profile's secret elsewhere, while being counted on the profile | closed (W4-04.1): cleaned environment before every call; measured with the real CLI against a fake API |
| 14 | Agents pass text to each other through the supervising agent's prompt history and its reasons, around chat and seals | closed (W4-04.1): the prompt shows only the asking agent's history |
| 15 | An agent writes a Markdown image with an outside address into chat or a transcript; the user's browser loads it and so sends data out without any approval | closed (W4-06.1): such images are shown as text; CSP `img-src 'self' data:` |
| 16 | Agent text runs JavaScript in the GUI (the Markdown library's safe mode left `<` after a letter raw, so an element with an event handler fired without a click); script there acts as the user, for example on approvals | closed (W4-06.2), present since W1-06: escaping and tag allowlist, plus CSP `script-src-attr 'none'`; `javascript:` links are held by the allowlist alone |
| 17 | Very large agent texts (transcripts, summaries) block the service for seconds while they are rendered | **open**, no code runs; follow-up planned (size limit or rendering off the event loop) |

Closing the ways we found does not prove that there are no others. Every review so far has found new ones.

### 5.3 `KERN_PROCARGS2`

`KERN_PROCARGS2` is a kernel `sysctl` on macOS that returns the command line and the start environment of a process. `ps` uses it. For processes of the same user it needs no special rights. Apple's own system binaries do not expose their environment, but ordinary applications do.

This matters because some applications keep credentials in their environment, and a process inside the sandbox can read them.

Seatbelt cannot block this call. We tested seven rules, applied after process start: `deny sysctl-read` by name, prefix and regex, `deny process-info*` and `(deny default)`. The call succeeded under all of them, while counter-tests showed that the rules themselves were active. `arr` therefore keeps its own secrets out of every environment (principle 6), but it cannot protect secrets that other applications put there. The only fix we know of is layer 7. The call is open only for processes of the same user, but we have not yet measured that a separate account closes it.

**Until then:** this can include the application from which `arr` is started. Real runs therefore stay supervised until layer 7 exists.

## 6. Other platforms (template, not implemented)

The principles and layers carry over. These are the parts that change. Nothing here is implemented or measured yet.

| Concern | macOS (implemented) | Linux / server (to design) |
|---|---|---|
| OS sandbox | Seatbelt, Codex sandbox | Landlock, seccomp, bubblewrap or namespaces; containers or micro-VMs on servers |
| Credential store | Keychain | Secret Service (libsecret), files with strict permissions, a secret manager |
| Other processes' environment | `KERN_PROCARGS2`, not blockable by Seatbelt | `/proc/<pid>/environ`; hide with a PID namespace, `hidepid` or a separate user |
| Separate user | planned spike | separate UID per run or per tenant, or one VM per run |
| Network | sandbox rules of the CLIs | network namespace, egress firewall with an allowlist |

On servers the Hugging Face lesson applies directly. A single allowed network path, such as a package proxy, is a target in its own right and needs the same adversarial testing as the sandbox.

## 7. Operating rules now

- Real runs are supervised. Hard problems run unsupervised only after layer 7.
- After stopping a run, check that none of its processes are left (way 4).
- No shared work folder for Claude and Codex agents in one run.
- Do not write permission rules by hand that point into another agent's folder (way 9) or that grant reading over an agent's own work folder: the latter makes it read-only for the Claude CLI.
- Agents cannot commit to git inside a run, because `.git` is write-protected for them. That is the price of the wall.
- Every change to sandbox, permissions or logins gets an escape test with a counter-test, and an independent review.
- Never test against real logins: test `HOME`, bait files, exit codes only. Every test that starts a real CLI runs in a lab with a `security` stub and no network except localhost (W4-04.1).

## 8. Reporting

Report security findings to the maintainer directly, not in a public issue.
