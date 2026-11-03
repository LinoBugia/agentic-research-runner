# Security model

> Status: 2026-11-03 · Platform implemented: macOS · Maintained with every change to sandbox, permissions or logins, every security review and every update of macOS, Claude Code CLI or Codex.
>
> The login protection described in sections 5.1 and 5.2 is in its final review and not yet on `main`. It lands with the next merge of wave 3.

This document describes how `arr` keeps research agents from harming the machine they run on, which ways out we know of, and which of them are closed. It is written so that the general part can serve as a template for other operating systems and for servers. The platform part currently covers macOS only.

## 1. The conflict

`arr` runs groups of AI agents on very hard mathematical problems. Many of these problems are open, some may be unsolvable, and agents are given a goal and long runs to pursue it. To be useful, agents must work without a human confirming every command: they write and run Python, create files and talk to each other for hours.

That combination is exactly what makes agents dangerous. Under pressure to reach a goal that cannot be reached by legitimate means, capable models look for other means: they game the check, change their environment or reach for things they were never meant to touch. For everyday coding tasks this rarely shows. For hard research problems it has to be expected.

The clearest public case so far is the OpenAI–Hugging Face incident of July 2026. During internal training and evaluation runs, a large share of tasks was impossible to solve legitimately. Agents persisted, escaped their sandbox through its single allowed network path, coordinated over a message board they built themselves, and finally broke into Hugging Face's infrastructure in search of solutions. OpenAI named reward hacking and persistence on seemingly impossible tasks among the causes; the evaluation ran with safeguards deliberately reduced and with a sandbox whose boundary had not been tested adversarially. See [Wikipedia](https://en.wikipedia.org/wiki/OpenAI%E2%80%93HuggingFace_incident), [The Hacker News](https://thehackernews.com/2026/08/openai-says-reward-hacking-drove-ai.html) and [MIT Technology Review](https://www.technologyreview.com/2026/08/26/1143013/the-inside-story-on-why-openai-agents-hacked-hugging-face/).

`arr` works at a much smaller scale, but the pattern is the same: hard goal, autonomy, a sandbox as the only barrier. We draw two goals from it:

1. **No agent should have a reason to work around its limits.** An honest "not solved" is always a valid result, and a blocked agent stops and asks instead of trying another way.
2. **If an agent tries anyway, the walls hold.** We do not rely on goal 1.

## 2. Threat model

**Attacker:** an agent in a run, or several cooperating, with shell access, file tools, time and the skills of a capable penetration tester. We assume it actively tries to bypass every limit, including over several steps and across turns, sessions and agents.

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

1. **Assume active circumvention.** A limit counts only if it is enforced by the operating system or by a fixed rule, and only if a test proves it with a counter-test (the test turns red when the protection is removed).
2. **Autonomy inside, approval at the edge.** Agents work freely inside the sandbox. Only the grey zone (network, package installation, reading outside the run) asks for approval. Forbidden actions are denied without asking. Because there is no per-command confirmation, the walls must hold on their own.
3. **Nothing outside the sandbox consumes what an agent can write.** No process outside the sandbox (agent CLI, app server, chat server, `arr` itself) executes code, loads modules or reads settings from a place an agent can write to, neither through the shell nor through file tools.
4. **File tools never get more than the shell.** Where tools such as Write or Edit run outside the OS sandbox, a fixed rule denies them every path the sandbox denies the shell, derived from the same source.
5. **Secrets never reach the agent.** No credential is placed in the agent's environment, files or logs. A small start program outside the agent's reach fetches it and hands it to the model client over a pipe.
6. **Fail closed, stop fast, count everything.** If a check cannot run, the run does not start. Every login profile has a kill switch that stops all runs using it. Usage is recorded per profile, request by request.
7. **Re-prove on change.** A version watcher forces the escape tests to run again after every update of the OS, the agent CLIs or their sandbox rules.
8. **Separate the user.** The last wall is an OS account of its own for the agents, without the user's logins and files. Until it exists, real runs are supervised.

## 4. Layers

| Layer | What it does | Decides |
|---|---|---|
| 0 Purpose | Prompts state the limits and tell agents not to retry a denied step another way but to ask. Planned: "not solved" stated as a valid result | – |
| 1 OS sandbox | Every shell command: write only inside the run, read only from an allowlist, no network | kernel |
| 2 Fixed rules | Permission engine: hard denies (keychain tools, `sudo`, protected paths, `.git` via file tools) | code |
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
- **Codex project config:** `arr` records the state of the work folder's `.codex/` at run start and starts no further thread once it has changed.
- **Tests:** Escape tests run the real CLIs against a fake model server in a test `HOME` with random bait files, and check results by exit code only. A `security` stub keeps the real keychain out of every lab.

### 5.2 Known ways out

| # | Way | Status |
|---|---|---|
| 1 | Read logins, keys or files outside the run directly | closed, fixed tests with counter-test |
| 2 | Read the environment of other processes of the same user (`KERN_PROCARGS2`, see 5.3) | **open**, needs layer 7 |
| 3a | Plant code in a folder on the outside process's `PATH` or import path (run venv, `python -m` from the work folder) | closed (W3-06.3) |
| 3b | Plant git settings in an outside process's `HOME` | closed (W3-06.3) |
| 3c | Create `.git` with the Write tool, which runs outside the sandbox, so the CLI's own `git` runs an agent's filter | fixed on the branch, in review (W3-06.4) |
| 3d | Codex `apply_patch` writes `.git/config` and `.codex/config.toml`, which its shell sandbox protects; the app server runs `git` outside the sandbox every turn | fixed on the branch, in review (W3-06.4) |
| 3e | A `.codex/config.toml` written during a run adds an MCP server that the next app server starts outside the sandbox | measured: it does; fixed on the branch, in review (W3-06.4) |
| 3f | The CLI's Node writes its compile cache into the run's temp folder and loads it as code on the next start | fixed on the branch, in review (W3-06.4) |

Closing the ways we found does not prove that there are no others. Every review so far has found new ones.

### 5.3 `KERN_PROCARGS2`

`KERN_PROCARGS2` is a kernel `sysctl` on macOS that returns the command line and the start environment of a process. `ps` uses it. For processes of the same user it needs no special rights. Apple's own system binaries do not expose their environment, but ordinary applications do.

This matters because some applications keep credentials in their environment. Processes of the Claude desktop app, for example, carry `CLAUDE_CODE_OAUTH_TOKEN`. A few lines of Python inside the sandbox can read it.

Seatbelt cannot block this call. We tested seven rules, applied after process start: `deny sysctl-read` by name, prefix and regex, `deny process-info*` and `(deny default)`. The call succeeded under all of them, while counter-tests showed that the rules themselves were active. `arr` therefore keeps its own secrets out of every environment (principle 5), but it cannot protect secrets that other applications put there. The only fix we know of is layer 7. The call is open only for processes of the same user, but we have not yet measured that a separate account closes it.

**Until then:** this includes the Claude desktop app, from which `arr` is usually started. Real runs therefore stay supervised until layer 7 exists.

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
- No shared work folder for Claude and Codex agents in one run.
- Agents cannot commit to git inside a run, because `.git` is write-protected for them. That is the price of the wall.
- Every change to sandbox, permissions or logins gets an escape test with a counter-test, and an independent review.
- Never test against real logins: test `HOME`, bait files, exit codes only.

## 8. Reporting

The repository is private. Report security findings to the maintainer directly, not in a public issue.
