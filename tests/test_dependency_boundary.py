"""Albert is the public data plane: it must build and run without private code.

Albert Cloud consumes Albert's released contracts, never the reverse. These checks
fail if public source starts importing, depending on, or pulling images or
packages from the private control plane or a private registry. Mentioning Albert
Cloud as a peer (a manifest's ``"cloud": "albert-cloud"`` identity, docs, copy) is
fine; only real dependency edges are refused.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
SKIPPED_DIRS = {
    ".git",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "test-results",
}
PRIVATE_NAME = re.compile(r"albert[-_]cloud", re.IGNORECASE)
# Packages under the @jiggai scope that are published publicly from this repo.
PUBLIC_SCOPED_PACKAGES = frozenset({"@jiggai/albert-openclaw-adapter"})
PUBLIC_REGISTRY_HOSTS = frozenset({"registry.npmjs.org", "pypi.org", "files.pythonhosted.org"})
JS_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}
JS_SPECIFIER = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)["']([^"']+)["']""")


def _files(*suffixes: str, names: tuple[str, ...] = ()) -> list[Path]:
    found = []
    for path in ROOT.rglob("*"):
        if any(part in SKIPPED_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.is_file() and (path.suffix in suffixes or path.name in names):
            found.append(path)
    return sorted(found)


def private_python_imports(source: str) -> list[str]:
    modules = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
    return [name for name in modules if name.split(".")[0] == "albert_cloud"]


def private_package_name(name: str) -> bool:
    if PRIVATE_NAME.search(name):
        return True
    return name.startswith("@jiggai/") and name not in PUBLIC_SCOPED_PACKAGES


def private_js_imports(source: str) -> list[str]:
    return [
        specifier
        for specifier in JS_SPECIFIER.findall(source)
        if not specifier.startswith((".", "/", "@/"))
        and private_package_name(_package_of(specifier))
    ]


def _package_of(specifier: str) -> str:
    parts = specifier.split("/")
    return "/".join(parts[:2]) if specifier.startswith("@") else parts[0]


def _requirement_name(requirement: str) -> str:
    return re.split(r"[\s\[<>=!~;@]", requirement.strip(), maxsplit=1)[0]


def private_python_requirements(pyproject: str, constraints: str) -> list[str]:
    project = tomllib.loads(pyproject).get("project", {})
    requirements = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)
    refused = [item for item in requirements if PRIVATE_NAME.search(_requirement_name(item))]
    for line in constraints.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-i", "--index-url", "--extra-index-url", "--find-links", "-f")):
            refused.append(line)
        elif PRIVATE_NAME.search(_requirement_name(line)):
            refused.append(line)
    return refused


def private_npm_dependencies(package_json: str) -> list[str]:
    manifest = json.loads(package_json)
    names = []
    for section in (
        "dependencies",
        "devDependencies",
        "optionalDependencies",
        "peerDependencies",
    ):
        names.extend(manifest.get(section, {}))
    return [name for name in names if private_package_name(name)]


def private_registry_urls(text: str) -> list[str]:
    return [
        url
        for url in re.findall(r"https?://[^\s\"'<>]+", text)
        if urlparse(url).hostname not in PUBLIC_REGISTRY_HOSTS
    ]


def lockfile_private_sources(package_lock: str) -> list[str]:
    refused = []
    for path, entry in json.loads(package_lock).get("packages", {}).items():
        resolved = entry.get("resolved")
        name = path.rsplit("node_modules/", 1)[-1]
        if (name and private_package_name(name)) or (
            resolved and urlparse(resolved).hostname not in PUBLIC_REGISTRY_HOSTS
        ):
            refused.append(f"{path} {resolved or ''}".strip())
    return refused


def private_image_references(text: str) -> list[str]:
    references = re.findall(r"(?im)^\s*(?:FROM\s+(?:--\S+\s+)*|image:\s*)(\S+)", text)
    return [reference for reference in references if PRIVATE_NAME.search(reference)]


def _relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


def test_public_python_never_imports_the_control_plane() -> None:
    violations = {
        _relative(path): imports
        for path in _files(".py")
        if (imports := private_python_imports(path.read_text()))
    }
    assert violations == {}


def test_public_javascript_never_imports_private_packages() -> None:
    violations = {
        _relative(path): imports
        for path in _files(*JS_SUFFIXES)
        if (imports := private_js_imports(path.read_text()))
    }
    assert violations == {}


def test_python_dependencies_come_from_public_indexes() -> None:
    assert (
        private_python_requirements(
            (ROOT / "pyproject.toml").read_text(), (ROOT / "constraints.txt").read_text()
        )
        == []
    )


def test_npm_dependencies_are_public() -> None:
    violations = {
        _relative(path): names
        for path in _files(names=("package.json",))
        if (names := private_npm_dependencies(path.read_text()))
    }
    assert violations == {}
    locks = {
        _relative(path): sources
        for path in _files(names=("package-lock.json",))
        if (sources := lockfile_private_sources(path.read_text()))
    }
    assert locks == {}


def test_no_private_registry_is_configured() -> None:
    violations = {
        _relative(path): urls
        for path in _files(names=(".npmrc", ".yarnrc", ".yarnrc.yml", "pip.conf", "uv.toml"))
        if (urls := private_registry_urls(path.read_text()))
    }
    assert violations == {}


def test_images_do_not_reference_the_control_plane() -> None:
    violations = {
        _relative(path): references
        for path in _files(".yaml", ".yml", names=("Dockerfile",))
        if (references := private_image_references(path.read_text()))
    }
    assert violations == {}


def test_checks_refuse_deliberate_violations() -> None:
    assert private_python_imports("from albert_cloud.service import X") == ["albert_cloud.service"]
    assert private_python_imports("import albert_cloud") == ["albert_cloud"]
    assert private_python_imports("from albert import cloud") == []
    assert private_js_imports("import { a } from '@jiggai/albert-cloud-sdk'") == [
        "@jiggai/albert-cloud-sdk"
    ]
    assert private_js_imports("const x = require('@jiggai/premium/sub')") == ["@jiggai/premium/sub"]
    assert private_js_imports("await import('albert-cloud')") == ["albert-cloud"]
    assert private_js_imports("import p from '@jiggai/albert-openclaw-adapter'") == []
    assert private_js_imports("import { x } from '@/lib/api'") == []
    pyproject = '[project]\ndependencies = ["albert-cloud>=1", "httpx>=0.28"]\n'
    assert private_python_requirements(pyproject, "") == ["albert-cloud>=1"]
    assert private_python_requirements(
        "[project]\n", "--extra-index-url https://pkgs.example.com/simple\n"
    ) == ["--extra-index-url https://pkgs.example.com/simple"]
    assert private_npm_dependencies('{"dependencies": {"@jiggai/connectors": "1"}}') == [
        "@jiggai/connectors"
    ]
    assert lockfile_private_sources(
        '{"packages": {"node_modules/x": {"resolved": "https://npm.pkg.github.com/x"}}}'
    ) == ["node_modules/x https://npm.pkg.github.com/x"]
    assert private_registry_urls("@jiggai:registry=https://npm.pkg.github.com\n") == [
        "https://npm.pkg.github.com"
    ]
    assert private_image_references("FROM ghcr.io/jiggai/albert-cloud:1\n") == [
        "ghcr.io/jiggai/albert-cloud:1"
    ]
    assert private_image_references("    image: ghcr.io/jiggai/albert_cloud-worker\n") == [
        "ghcr.io/jiggai/albert_cloud-worker"
    ]
    assert private_image_references("FROM python:3.12-slim\n") == []
