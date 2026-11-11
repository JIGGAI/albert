from pathlib import Path

SKILL = Path(__file__).parents[1] / "skills" / "albert-connect" / "SKILL.md"


def test_agent_connect_skill_has_secure_supported_flow() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert text.startswith("---\nname: albert-connect\n")
    assert "albert-memory==0.4.1" in text
    assert "gh attestation verify" in text
    assert "albert-capture connect\n" in text
    assert "<connector> doctor --smoke" in text
    assert "OpenClaw `>=2026.9.6,<2027.0.0`" in text
    assert "albert-claude-lifecycle connect\n" in text
    assert "albert-codex-lifecycle connect\n" in text
    assert "Claude Code `>=2.1.78,<2.2.0` | Supported" in text
    assert "Codex `>=0.166.0` | Supported" in text
    assert "Cursor | Not yet supported" in text
    reuse = text.index("## 3. Reuse a healthy matching connection")
    install = text.index("## 4. Install the exact verified connector when missing")
    reuse_contract = text[reuse:install]
    assert 'importlib.metadata.version("albert-memory")' in reuse_contract
    assert "installed `albert-memory` version is exactly `0.4.1`" in reuse_contract
    assert '"package_version": "0.4.1"' in reuse_contract


def test_agent_connect_skill_forbids_secret_bearing_shortcuts() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert "Never echo it" in text
    assert "never pass the link as a command argument" in text
    assert "connect --link-stdin <<< '<the link from the user's message>'" in text
    assert "do not retry with the same link" in text
    assert "Never fall back to an unpinned package" in text
    assert "albert-capture connect <" not in text
    assert "--link " not in text


def test_agent_connect_skill_reuses_only_the_exact_package_version() -> None:
    text = SKILL.read_text(encoding="utf-8")

    version_check = text.index('"package_version": "0.4.1"')
    doctor = text.index("<connector> doctor --smoke")
    reuse = text.index("keep the existing connection")
    assert version_check < doctor < reuse
    assert "do not trust executable names or\nthe Brain origin alone" in text
