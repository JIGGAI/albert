from pathlib import Path

SKILL = Path(__file__).parents[1] / "skills" / "albert-connect" / "SKILL.md"


def test_agent_connect_skill_has_secure_supported_flow() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert text.startswith("---\nname: albert-connect\n")
    assert "albert-memory==0.4.0" in text
    assert "gh attestation verify" in text
    assert "albert-capture connect\n" in text
    assert "albert-capture doctor --smoke" in text
    assert "OpenClaw `>=2026.9.6,<2027.0.0`" in text
    assert "Codex | Not yet supported" in text
    assert "Claude Code | Not yet supported" in text
    assert "Cursor | Not yet supported" in text


def test_agent_connect_skill_forbids_secret_bearing_shortcuts() -> None:
    text = SKILL.read_text(encoding="utf-8")

    assert "Never echo it" in text
    assert "Do not include the link in the command" in text
    assert "Never fall back to an unpinned package" in text
    assert "albert-capture connect <" not in text
    assert "--link " not in text
