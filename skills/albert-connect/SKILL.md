---
name: albert-connect
description: Securely connect an agent host to an Albert Brain using a private one-time link. Use when a user asks to connect Albert, install Albert Memory, redeem an Albert Cloud link, or verify an existing Albert connection.
---

# Connect to Albert

## 1. Protect the one-time link

- Treat the link as a password until setup succeeds.
- Never echo it, place it in command arguments or environment variables, save it in notes or files, or include it in logs or the final report. The only shell form allowed is the here-string in step 5.
- Accept it only through an interactive hidden prompt, private standard input supplied directly by the tool API, or an already-open private file descriptor.

**Complete when:** the link remains only in the current private conversation/tool input and has not been copied to another persistence surface.

## 2. Detect the host before changing it

Check for the host and its version without using the link:

1. `openclaw --version`
2. `codex --version`
3. `claude --version`
4. Cursor's installed application/CLI metadata

Use the first host that owns the current agent session. Do not infer support merely because an unrelated executable is installed.

Current Cloud one-time-link support. Each link is signed for one host, so use the host the link was made for:

| Host | Cloud link | Connector command | Owned marker |
| --- | --- | --- | --- |
| OpenClaw `>=2026.9.6,<2027.0.0` | Supported | `albert-capture` | `~/.openclaw/albert/install.json` |
| Claude Code `>=2.1.78,<2.2.0` | Supported | `albert-claude-lifecycle` | `~/.claude/albert/install.json` |
| Codex `>=0.166.0` | Supported | `albert-codex-lifecycle` | `$CODEX_HOME/albert/install.json` (default `~/.codex`) |
| Cursor | Not yet supported | — | Do not install or redeem; report that the production lifecycle target is not available yet. |
| Generic MCP/REST client | Custom-client path only | — | Do not substitute it for automatic memory capture; ask the user to choose "Other (MCP/REST)" in Albert Cloud. |

Below, `<connector>` means the connector command for the detected host and `<marker>` its owned marker. Do not install one host's adapter into another host; a link made for a different host is refused by `connect` before redemption.

**Complete when:** the current host and version are known, and unsupported hosts have stopped without consuming the link.

## 3. Reuse a healthy matching connection

If `<connector>` already exists, inspect both the installed distribution metadata and the owned marker at `<marker>` without using the link:

```bash
python -c 'import importlib.metadata; print(importlib.metadata.version("albert-memory"))'
```

A reusable installation must prove that the installed `albert-memory` version is exactly `0.4.1` and record both `"owner": "albert-memory"` and `"package_version": "0.4.1"` in the marker (Claude Code and Codex record it under `"cloud"`). A missing or different package version requires the verified installation step below; do not trust executable names or
the Brain origin alone.

Only when both exact-version checks pass, run:

```bash
<connector> doctor --smoke
```

If doctor passes and the marker has the same Brain API origin encoded by the link (the portion before `/onboard/`), keep the existing connection and do not redeem the new link. Inspect only `install.json`; never read or print the credential file.

If doctor fails, either exact-version check fails, the Brain differs, or the user explicitly requested reconnection/rotation, continue. Do not uninstall the working connection first; `connect` performs transactional replacement and rollback.

**Complete when:** a healthy matching exact-version connection is reused, or replacement is confirmed necessary.

## 4. Install the exact verified connector when missing

The supported connector package for this skill revision is `albert-memory==0.4.1`. The copied Albert Cloud prompt must name the same expected package version. Stop on a mismatch.

If `<connector>` is missing or its installed package version differs:

1. Create a private temporary directory.
2. Download only the exact wheel, without installing it:

   ```bash
   python -m pip download --no-deps --only-binary=:all: --dest <private-temp-dir> albert-memory==0.4.1
   ```

3. Verify its GitHub build attestation before installation:

   ```bash
   gh attestation verify <downloaded-wheel> --repo JIGGAI/albert
   ```

4. Install that verified local wheel with `uv tool install <downloaded-wheel>` or `pipx install <downloaded-wheel>`.
5. Remove only the temporary download directory after installation.
6. Confirm `<connector> --help` succeeds and the installed `albert-memory` version is exactly `0.4.1`.

If the exact wheel is unpublished, unavailable, unattested, or cannot be verified, stop. Never fall back to an unpinned package, a floating Git branch, or source copied from the one-time-link endpoint.

**Complete when:** the exact attested package is installed as a tool and `<connector>` resolves from the current executable path.

## 5. Connect privately

Preferred interactive path, for the detected host:

```bash
albert-capture connect
albert-claude-lifecycle connect
albert-codex-lifecycle connect
```

Run only the one line for the detected host. Prefer, in order:

1. **Hidden prompt.** If your execution tool offers a PTY or hidden input, run `<connector> connect`, wait for `Albert Cloud one-time link:`, and type the link there.
2. **Private stdin.** If your tool can send standard input directly, run `<connector> connect --link-stdin` and send the link through that API.
3. **Here-string.** Agents whose shell tool cannot do either (Claude Code, Codex) run exactly:

   ```bash
   <connector> connect --link-stdin <<< '<the link from the user's message>'
   ```

   A here-string is handled by the shell itself, so the link never appears in a process argument list. This is acceptable only because the link is already in this conversation, works once, expires in 15 minutes, and is dead the moment `connect` redeems it. Do not use `echo` or `printf` pipelines, environment variables, files, or command substitution, and never pass the link as a command argument.

Use `--link-fd` only when the caller already supplied a private open descriptor. If `connect` fails, do not retry with the same link: a redeemed link is spent. Ask the user for a new one.

`connect` must validate the signed manifest, its host family, package provenance, host compatibility, one-time redemption identity, Brain readiness, live capture, and fresh recall before committing. On failure, preserve the prior working installation and report the sanitized error; the issued credential is revoked automatically.

**Complete when:** `connect` reports that live capture and fresh recall passed.

## 6. Verify and report

Run:

```bash
<connector> doctor --smoke
```

For OpenClaw also run `openclaw plugins inspect albert-memory --runtime --json`. For Claude Code, tell the user to start Claude Code with `~/.claude/albert/bin/albert-claude`. For Codex, tell the user to review and trust the new hooks in Codex.

Report only:

- host and version;
- `albert-memory` package version;
- connected Brain API origin and workspace identifier from `install.json`;
- plugin runtime health (OpenClaw);
- capture/recall smoke result;
- whether an existing connection was reused or transactionally replaced;
- the host's next step above.

Never report the link, bearer credential, queue key, compensation receipt, or credential-file contents.

**Complete when:** doctor (and OpenClaw runtime inspection) pass and the user receives a sanitized connection summary.
