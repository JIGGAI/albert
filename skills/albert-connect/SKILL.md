---
name: albert-connect
description: Securely connect an agent host to an Albert Brain using a private one-time link. Use when a user asks to connect Albert, install Albert Memory, redeem an Albert Cloud link, or verify an existing Albert connection.
---

# Connect to Albert

## 1. Protect the one-time link

- Treat the link as a password until setup succeeds.
- Never echo it, quote it in a shell command, place it in command arguments or environment variables, save it in notes, or include it in logs or the final report.
- Accept it only through an interactive hidden prompt, private standard input supplied directly by the tool API, or an already-open private file descriptor.

**Complete when:** the link remains only in the current private conversation/tool input and has not been copied to another persistence surface.

## 2. Detect the host before changing it

Check for the host and its version without using the link:

1. `openclaw --version`
2. `codex --version`
3. `claude --version`
4. Cursor's installed application/CLI metadata

Use the first host that owns the current agent session. Do not infer support merely because an unrelated executable is installed.

Current Cloud one-time-link support:

| Host | Cloud link | Action |
| --- | --- | --- |
| OpenClaw `>=2026.9.6,<2027.0.0` | Supported | Continue below. |
| Codex | Not yet supported | Do not redeem the link; report that direct lifecycle setup exists but Cloud-link onboarding is not unified yet. |
| Claude Code | Not yet supported | Do not redeem the link; report that direct lifecycle setup exists but Cloud-link onboarding is not unified yet. |
| Cursor | Not yet supported | Do not install or redeem; report that the production lifecycle target is not available yet. |
| Generic MCP/REST client | Custom-client path only | Do not substitute it for automatic memory capture; ask the user to use Albert Cloud's advanced custom-client flow. |

Do not install the OpenClaw adapter into another host.

**Complete when:** the current host and version are known, and unsupported hosts have stopped without consuming the link.

## 3. Reuse a healthy matching OpenClaw connection

If `albert-capture` already exists, inspect only the owned marker at
`~/.openclaw/albert/install.json`. A reusable installation must record both
`"owner": "albert-memory"` and `"package_version": "0.4.0"`. A missing or different
package version requires the verified installation step below; do not trust executable names or
the Brain origin alone.

Only when the marker records the exact package version, run:

```bash
albert-capture doctor --smoke
```

If doctor passes and the marker has the same Brain API origin encoded by the link (the portion
before `/onboard/`), keep the existing connection and do not redeem the new link. Inspect only
`install.json`; never read or print `credentials.env`.

If doctor fails, the Brain differs, or the user explicitly requested reconnection/rotation, continue. Do not uninstall the working connection first; `connect` performs transactional replacement and rollback.

**Complete when:** a healthy matching exact-version connection is reused, or replacement is
confirmed necessary.

## 4. Install the exact verified connector when missing

The supported connector package for this skill revision is `albert-memory==0.4.0`. The copied Albert Cloud prompt must name the same expected package version. Stop on a mismatch.

If `albert-capture` is missing or its installed package version differs:

1. Create a private temporary directory.
2. Download only the exact wheel, without installing it:

   ```bash
   python -m pip download --no-deps --only-binary=:all: --dest <private-temp-dir> albert-memory==0.4.0
   ```

3. Verify its GitHub build attestation before installation:

   ```bash
   gh attestation verify <downloaded-wheel> --repo JIGGAI/albert
   ```

4. Install that verified local wheel with `uv tool install <downloaded-wheel>` or `pipx install <downloaded-wheel>`.
5. Remove only the temporary download directory after installation.
6. Confirm `albert-capture --help` succeeds and the installed `albert-memory` version is exactly `0.4.0`.

If the exact wheel is unpublished, unavailable, unattested, or cannot be verified, stop. Never fall back to an unpinned package, a floating Git branch, or source copied from the one-time-link endpoint.

**Complete when:** the exact attested package is installed as a tool and `albert-capture` resolves from the current executable path.

## 5. Connect privately

Preferred interactive path:

```bash
albert-capture connect
```

Wait for `Albert Cloud one-time link:` and provide the link through the tool's hidden/PTY input. Do not include the link in the command.

When an interactive prompt is impossible, invoke `albert-capture connect --link-stdin` and send the link directly through the execution tool's private stdin API. Do not build a shell pipeline with `echo`, `printf`, a here-document, command substitution, or an environment variable. Use `--link-fd` only when the caller already supplied a private open descriptor.

`connect` must validate the signed manifest, package provenance, host compatibility, one-time redemption identity, Brain readiness, plugin reload, live capture, and fresh recall before committing. On failure, preserve the prior working installation and report the sanitized error.

**Complete when:** `connect` reports that reload, live capture, and fresh recall passed.

## 6. Verify and report

Run:

```bash
albert-capture doctor --smoke
openclaw plugins inspect albert-memory --runtime --json
```

Report only:

- host and version;
- `albert-memory` package version;
- connected Brain API origin and workspace identifier from `install.json`;
- plugin runtime health;
- capture/recall smoke result;
- whether an existing connection was reused or transactionally replaced.

Never report the link, bearer credential, queue key, compensation receipt, or credential-file contents.

**Complete when:** doctor and runtime inspection pass and the user receives a sanitized connection summary.
