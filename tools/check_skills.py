#!/usr/bin/env python3
"""Check that skills/, the plugin manifest, the skill indexes and the README badges agree.

Checks: frontmatter `name` == directory name (SKILL.md and SKILL.zh.md); every skill has a
marketplace.json entry with source ./skills/<name>; every skill is listed in SKILLS.md and
SKILLS.en.md; the skills badge in README.md / README.en.md shows the right count.

Usage: python3 tools/check_skills.py [REPO_ROOT]   (exit code 1 on any mismatch)
REPO_ROOT defaults to the repository containing this script.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent


def git_ignored(path: Path) -> bool:
    result = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=ROOT)
    return result.returncode == 0


def frontmatter_name(skill_md: Path) -> str:
    match = re.search(r"^name:\s*(\S+)\s*$", skill_md.read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else ""


def main() -> int:
    errors = []

    skills = {
        d.name
        for d in (ROOT / "skills").iterdir()
        if (d / "SKILL.md").is_file() and not git_ignored(d)
    }
    for name in sorted(skills):
        for md in ("SKILL.md", "SKILL.zh.md"):
            path = ROOT / "skills" / name / md
            if path.is_file() and frontmatter_name(path) != name:
                errors.append(f"skills/{name}/{md}: frontmatter name '{frontmatter_name(path)}' != directory name")

    manifest = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    plugins = {p["name"]: p["source"] for p in manifest["plugins"]}
    for name in sorted(skills - plugins.keys()):
        errors.append(f"marketplace.json: missing plugin entry for skills/{name}")
    for name, source in sorted(plugins.items()):
        if name not in skills:
            errors.append(f"marketplace.json: plugin '{name}' has no skills/{name}/SKILL.md")
        elif source != f"./skills/{name}":
            errors.append(f"marketplace.json: plugin '{name}' source is '{source}', expected './skills/{name}'")

    for index in ("SKILLS.md", "SKILLS.en.md"):
        text = (ROOT / index).read_text(encoding="utf-8")
        indexed = set(re.findall(r"^\| \[`([\w-]+)`\]\(skills/", text, re.M))
        for name in sorted(skills - indexed):
            errors.append(f"{index}: skill index is missing '{name}'")
        for name in sorted(indexed - skills):
            errors.append(f"{index}: skill index lists unknown skill '{name}'")

    for readme in ("README.md", "README.en.md"):
        text = (ROOT / readme).read_text(encoding="utf-8")
        badge = re.search(r"badge/skills-(\d+)-", text)
        if not badge or int(badge.group(1)) != len(skills):
            errors.append(f"{readme}: skills badge should be {len(skills)}")

    for error in errors:
        print(f"ERROR {error}")
    print(f"{len(skills)} skills checked, {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
