# ADR-0002: Python runtime and optional official adapter SDKs

Date: 2026-10-03. Status: accepted for v0.1.

## Comparison

| Criterion | Go | Rust | TypeScript | Python |
| --- | --- | --- | --- | --- |
| Official MCP SDK | Tier 1 | Tier 2 | Tier 1 | Tier 1 |
| Official Slack SDK | community integration | community integration | Bolt | Bolt |
| SQLite | external driver, pure-Go or C | external crate | runtime/binding choice | standard library |
| Single binary | excellent | excellent | requires bundling/runtime | OS-specific bundle |
| OS / Docker | strong | strong | strong | strong |
| Adapter authoring / tests | strong | larger implementation cost | strong | strong, stdlib tests |
| Initial dependency surface | driver + SDK | crates + SDK | runtime + SDK | Core: standard library only |
| Edge adapter future | HTTP contract | HTTP contract | particularly convenient | HTTP contract |

Use Python 3.12+ consistently. Core is independent of all SDKs, with an internal
Service API as the source of truth. SQLite's built-in binding and official MCP
and Slack SDKs reduce integration risk for this single-profile, I/O-bound hub.
Go would be the strongest alternative if compact native binaries were the only
priority. Rust is not inherited from another project. A future Edge adapter may
use another language without changing the Core protocol.

Tradeoff: distributable executables bundle a runtime and must be built and tested
on each supported OS. PyInstaller **onedir** bundles are the first packaging
candidate; native single-file output is not a verified feature of this milestone.
Wheel and Docker are additive distribution methods. No package publication,
registry reservation, binary signing, or release is authorized by this ADR.

MCP: pinned official `mcp==1.29.0` v1 SDK, stdio initially. Do not silently adopt
v2 APIs from current main-branch documentation. Remote MCP requires a separate
OAuth design and is disabled. Slack: optional official Bolt Python / Slack SDK,
Socket Mode candidate; no OAuth or production posting is implied.

## Sources checked

- https://modelcontextprotocol.io/docs/sdk
- https://github.com/modelcontextprotocol/python-sdk/tree/v1.29.0 (MIT)
- https://docs.slack.dev/tools/bolt-python/concepts/socket-mode (MIT SDK)
- https://docs.python.org/3.12/library/sqlite3.html (PSF runtime)
- https://pyinstaller.org/en/stable/operating-mode.html
- https://pyinstaller.org/en/stable/license.html (GPL exception permits bundled applications)

Dependency licenses and transitive notices must be inventoried before distributing
bundles. No code is copied from MANY-AI-CLI or Deskly.
