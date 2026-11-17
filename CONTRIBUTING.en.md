# Contributing Guide

[← Back to README](README.en.md) · [中文](CONTRIBUTING.md)

This guide explains how to add or change a skill in `aminer-research-skills`, and how pull requests are submitted and handled.

---

## 1. Directory layout

Each skill lives under `skills/<skill-name>/`:

```
skills/<skill-name>/
├── SKILL.md               # English skill definition (required)
├── SKILL.zh.md            # Chinese version (recommended; keep in sync with the English one)
├── commands/
│   └── <command-name>.md  # Slash command entry (only if the skill provides a slash command)
├── requirements.txt       # Python dependencies (required if there are third-party dependencies)
└── ...                    # scripts/, references/, evals/, etc.
```

The directory name is the skill name: the frontmatter `name` in `SKILL.md` (and `SKILL.zh.md`) must match the directory name exactly. Both `npx skills --skill` and Claude Code plugin installation identify skills by it. The `aminer-` prefix is not required (e.g. `baseline-finder`, `sota-finder`); use lowercase letters, digits, and hyphens.

### 1.1 Files to update when adding a skill

- **`.claude-plugin/marketplace.json`**: append an entry to the `plugins` array:

  ```json
  {
    "name": "<skill-name>",
    "description": "One-line description",
    "source": "./skills/<skill-name>",
    "strict": false
  }
  ```

  Do not change `metadata.version` or the version badge at the top of the README; maintainers bump versions on release.

- **`SKILLS.md` (Chinese) / `SKILLS.en.md` (English)**: update both, keeping their structure 1:1.
  - Add a row to the table of the matching category (Search and discovery / Reading and organizing / Research reports / Manuscript coverage checks / Citation checking). The Chinese file links to `SKILL.zh.md` (or `SKILL.md` if there is no Chinese version); the English file links to `SKILL.md`:

    ```
    | [`<skill-name>`](skills/<skill-name>/SKILL.md) | Draft / Beta / Preview / Stable | One-line purpose | "trigger phrase" | Token requirement |
    ```

  - Add an entry under that category's "Example prompts".
  - If the skill is easily confused with an existing one, add a row to "Choosing between similar skills".
  - Increment the total skill count in the opening paragraph.
  - If no existing category fits, add a new one and update the contents list at the top.

- **`README.md` / `README.en.md`**:
  - Increment the skills badge count at the top (`badge/skills-<N>-`).
  - Under "Local skills", add a link line with a one-sentence description in the matching category, and update the skill count at the start of that section.

- **`INSTALL.md` / `INSTALL.en.md`**: if the skill needs third-party Python packages, add them to the "4. Python dependencies" table.

### 1.2 Status labels

| Label | Meaning |
|-------|---------|
| `Draft` | Rules are defined but not yet tested on real cases |
| `Beta` | Tested on examples; edge cases may remain |
| `Preview` | Newly released and usable; triggers and output format may still change |
| `Stable` | Validated on real academic content; rules are relatively stable |

---

## 2. SKILL.md conventions

### 2.1 Frontmatter

```yaml
---
name: <skill-name>              # required; must match the directory name
name_zh: 中文显示名              # recommended
version: 1.0.0                  # recommended
author: AMiner                  # recommended
contact: report@aminer.cn       # recommended
description: >                  # required
  [Activation] When this skill should be activated.
  [Scope] What it concretely does.
  [Routing] Its boundary with other skills (when NOT to use it).
homepage: https://github.com/AMinerOrg/aminer-research-skills/tree/main/skills/<skill-name>
metadata:
  {
    "openclaw":
      {
        "emoji": "...",
        "requires": {
          "bins": ["python3"],
          "env": ["AMINER_API_KEY"]
        },
        "primaryEnv": "AMINER_API_KEY"
      }
  }
---
```

`name` and `description` are required; `version` / `author` / `contact` are recommended.

**Three-part `description`**: activation → scope → routing (when to hand off to another skill). All three are required. Existing skills are good references for wording.

**`metadata.openclaw.requires`**:
- `bins`: executables the scripts need (e.g. `["python3"]`)
- `env`: environment variables that must exist at runtime. List only the truly required ones (optional model keys and other variables checked dynamically at runtime do not belong here)
- `primaryEnv`: the name of the main key

### 2.2 Required body content

**Environment check**: check required keys before doing anything else, using this format:

```bash
[ -z "${AMINER_API_KEY+x}" ] && echo "AMINER_API_KEY missing" || echo "AMINER_API_KEY exists"
```

If a key is missing, stop immediately and direct the user to https://open.aminer.cn to get a token. **Never print a key's value in any output.**

**Usage example**: give a bash command that runs as-is, with default parameters where possible, so readers can see at a glance how to run it.

**Runtime constraints**: list the rules the model or agent must follow while running (budget limits, no fabricated data, billing reminders, etc.).

**File Map** (multi-file skills): briefly describe what each file does.

---

## 3. commands/<command-name>.md conventions

Only needed if the skill provides a slash command. Command files follow a four-part structure:

```markdown
---
description: One-line description
argument-hint: [argument format hint]
allowed-tools: Read, Bash, Glob, Grep
---

# /command-name — Title

User invoked ... with the following arguments:
$ARGUMENTS

## Your task

### 1. Pre-flight
[Check environment variables and dependencies; stop on any failure without running scripts]

### 2. Parse `$ARGUMENTS`
[Which fields to extract from the arguments, and their defaults]

### 3. Run
[A command that can be copied and run directly]

### 4. Present the result
[How to present the output to the user]
```

`Pre-flight` must at least check every required environment variable. Skills with Python scripts should also check that dependencies are installed:

```bash
python3 - <<'PY'
import importlib.util
missing = [name for name in ("package1", "package2") if importlib.util.find_spec(name) is None]
print("Missing: " + ", ".join(missing) if missing else "Python dependencies exist")
PY
```

---

## 4. Python code conventions

### 4.1 Do not hardcode keys or provider details

```python
# Wrong: hardcoded key or base_url
client = OpenAI(api_key="sk-xxx", base_url="https://some-provider.com/v1")

# Right: read from the environment and fail loudly instead of silently
api_key = os.getenv("BIGMODEL_API_KEY")
if not api_key:
    raise ValueError("BIGMODEL_API_KEY is required. Set it in the environment or pass --api-key.")
```

Neither `AMINER_API_KEY` nor model keys may appear in code; read them only from environment variables or command-line arguments. Use provider-prefixed variable names for model keys (e.g. `BIGMODEL_API_KEY`, `DEEPSEEK_API_KEY`, `GLM_API_KEY`). A base_url or model name may have a default, but it must be overridable via an environment variable (e.g. `DEEPSEEK_BASE_URL`, `LLM_MODEL`) or an argument.

### 4.2 Put shared helpers in a common module

When several scripts need the same helpers (ID extraction, deduplication, batched requests, etc.), move them into one common module (e.g. `_utils.py`) and import it everywhere instead of copying:

```python
# Wrong: a separate dedupe_preserve_order in both module_a.py and module_b.py

# Right: define it in _utils.py and import it in both
from _utils import dedupe_preserve_order, extract_paper_id
```

### 4.3 `__all__` lists only functions defined in the module

Do not add functions imported from other modules to `__all__`:

```python
# Wrong: normalize_paper_detail is actually defined in _utils.py
__all__ = ["search_papers", "normalize_paper_detail"]

# Right: list only what this file defines
__all__ = ["search_papers", "search_adding"]
```

### 4.4 Do not keep unused parameters or fields

If a refactor leaves a parameter unused, delete it rather than just changing its default:

```python
# Wrong: api_type is stored but never read
self.api_type = api_type

# Right: remove the parameter
```

### 4.5 requirements.txt format

Use `>=` for minimum versions; do not pin exact patch versions:

```
openai>=1.68.0
requests>=2.31.0
```

---

## 5. Pre-PR checklist

**Files**
- [ ] `SKILL.md` exists, its frontmatter `name` matches the directory name, and `description` has all three parts
- [ ] Skills that provide a slash command have `commands/<command-name>.md`
- [ ] The skill is registered in `.claude-plugin/marketplace.json` (`source: ./skills/<skill-name>`)
- [ ] `SKILLS.md` and `SKILLS.en.md` are updated (table row, example prompt, total count)
- [ ] `README.md` and `README.en.md` are updated (skills badge, local skills list)
- [ ] New third-party dependencies are in `requirements.txt` and in the dependency table of `INSTALL.md` / `INSTALL.en.md`
- [ ] **Required**: `python3 tools/check_skills.py` reports 0 problems. It checks that the directory name matches the frontmatter `name` in `SKILL.md` and `SKILL.zh.md`; every skill has a `marketplace.json` entry whose `source` is `./skills/<skill-name>`; every skill appears in the `SKILLS.md` and `SKILLS.en.md` index tables; and the skills badge count in `README.md` / `README.en.md` matches the actual number of skills

**SKILL.md content**
- [ ] `description` covers activation, scope, and routing
- [ ] The body has the bash environment check
- [ ] The body has a runnable usage example
- [ ] No hardcoded keys; base_url and model names are overridable via environment variables or arguments

**Code quality**
- [ ] Helpers shared across files live in a common module, not copied between files
- [ ] `__all__` lists only functions defined in the module
- [ ] No parameters or fields that are stored but never read

**Secrets**
- [ ] No plaintext keys anywhere in code, scripts, or test data
- [ ] `SKILL.md` and `commands/` files contain no instruction that prints a key value

---

## 6. How to submit

1. Fork the repository and create a branch from the latest `main` in your fork (e.g. `feat/<skill-name>`, `fix/<skill-name>-xxx`).
2. Make your changes and go through the checklist above.
3. Open a PR on GitHub against `main` of `AMinerOrg/aminer-research-skills`, describing what changed and how you verified it.

**PR titles** follow Conventional Commits. The scope is usually the skill name; the description may be in English or Chinese:

```
feat(sota-finder): support CSV leaderboard export
fix(aminer-pdf-ocr): fix upload timeout on large files
docs(readme): clarify Python dependencies
refactor(skills): extract shared request helper
```

**Also**:
- One topic per PR; split unrelated changes into separate PRs.
- Never commit a token or any other secret. Local config such as `.env` and `config.local.yaml` is already in `.gitignore`; do not force-add it. Do not commit runtime output such as `outputs/`.
- Draft PRs are not picked up for review; mark the PR "Ready for review" when it is ready.

---

## 7. How PRs are handled

PRs in this repository are **not merged with GitHub's merge button**. They go through an internal review process instead:

1. Once opened, a PR is automatically synced into the internal review process and reviewed by maintainers.
2. Further pushes to the PR branch are picked up automatically; there is no need to open a new PR.
3. Once accepted, the change lands on `main` as a commit authored by **AMiner Bot**, with you credited through a `Co-authored-by` trailer, so it still counts toward your GitHub contributions.
4. The PR is then closed automatically with a comment linking that commit. A PR showing Closed rather than Merged is expected.

**If the bot comments on your PR**:
- **It conflicts with `main`**: rebase your branch onto the latest `main`, resolve the conflicts, and push again (`git push --force-with-lease`).
- **It touches restricted paths**: remove the changes to those paths from the PR and push again.
