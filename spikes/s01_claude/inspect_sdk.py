"""Print the SDK types the spike relies on (no model calls).

Run: uv run --group spikes python spikes/s01_claude/inspect_sdk.py
"""

import dataclasses
import typing

import claude_agent_sdk
import claude_agent_sdk.types as t

TYPES = [
    "SandboxSettings",
    "SandboxNetworkConfig",
    "SandboxIgnoreViolations",
    "SdkPluginConfig",
    "SystemPromptPreset",
    "SystemPromptFile",
    "SystemPromptCustom",
    "ThinkingConfigAdaptive",
    "ToolPermissionContext",
    "PermissionResultAllow",
    "PermissionResultDeny",
    "PreToolUseHookInput",
    "PreToolUseHookSpecificOutput",
    "PostToolUseHookInput",
    "PostToolUseHookSpecificOutput",
    "HookMatcher",
    "McpStdioServerConfig",
]


def describe(name: str) -> None:
    obj = getattr(t, name)
    if dataclasses.is_dataclass(obj):
        hints = {f.name: f.type for f in dataclasses.fields(obj)}
    else:
        try:
            hints = typing.get_type_hints(obj)
        except Exception:
            hints = getattr(obj, "__annotations__", {})
    print(f"## {name}")
    for key, value in hints.items():
        print(f"  {key}: {value}")
    print()


def main() -> None:
    print(f"claude-agent-sdk {claude_agent_sdk.__version__}\n")
    print("## ClaudeAgentOptions")
    for field in dataclasses.fields(claude_agent_sdk.ClaudeAgentOptions):
        print(f"  {field.name}: {field.type}")
    print()
    for name in TYPES:
        describe(name)
    doc = claude_agent_sdk.ClaudeAgentOptions.__doc__ or ""
    print("## ClaudeAgentOptions.__doc__ (env/auth related lines)")
    for line in doc.splitlines():
        if any(k in line.lower() for k in ("env", "token", "auth", "api_key")):
            print("  " + line.strip())


if __name__ == "__main__":
    main()
