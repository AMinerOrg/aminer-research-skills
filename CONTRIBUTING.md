# 贡献指南

[← 返回 README](README.md) · [English](CONTRIBUTING.en.md)

本文档说明如何向 `aminer-research-skills` 添加或修改技能，以及 PR 的提交与处理流程。

---

## 一、目录结构

每个技能放在 `skills/<skill-name>/` 下：

```
skills/<skill-name>/
├── SKILL.md               # 英文技能定义（必须）
├── SKILL.zh.md            # 中文版本（推荐，与英文内容保持同步）
├── commands/
│   └── <command-name>.md  # slash command 入口（仅当技能提供 slash command 时需要）
├── requirements.txt       # Python 依赖（有第三方依赖时必须）
└── ...                    # scripts/、references/、evals/ 等
```

目录名即技能名：`SKILL.md`（以及 `SKILL.zh.md`）frontmatter 中的 `name` 必须与目录名完全一致，`npx skills --skill` 和 Claude Code 插件安装都按它识别技能。技能名不要求 `aminer-` 前缀（如 `baseline-finder`、`sota-finder`），使用小写字母、数字和连字符即可。

### 1.1 新增技能需要同步更新的文件

- **`.claude-plugin/marketplace.json`**：在 `plugins` 数组中追加一项：

  ```json
  {
    "name": "<skill-name>",
    "description": "一句话描述",
    "source": "./skills/<skill-name>",
    "strict": false
  }
  ```

  不要修改 `metadata.version`，也不要修改 README 顶部的 version 徽章，版本号由维护者在发布时统一更新。

- **`SKILLS.md`（中文）/ `SKILLS.en.md`（英文）**：两份同步修改，结构保持一一对应。
  - 在对应类别（检索与发现 / 阅读与整理 / 调研报告 / 稿件覆盖核查 / 引用核查）的表格中加一行。中文版链接到 `SKILL.zh.md`（没有中文版时链接 `SKILL.md`），英文版链接到 `SKILL.md`：

    ```
    | [`<skill-name>`](skills/<skill-name>/SKILL.zh.md) | Draft / Beta / Preview / Stable | 一句话用途 | 「触发词」 | Token 要求 |
    ```

  - 在该类别的「示例提示词」下补一条。
  - 与已有技能容易混淆时，在「易混技能如何选择」表中补一行区分。
  - 开头的技能总数加一。
  - 现有类别都不合适时，新增类别需同时更新文首目录。

- **`README.md` / `README.en.md`**：
  - 顶部 skills 徽章数字（`badge/skills-<N>-`）加一。
  - 在「本地技能」对应类别下加一行链接和一句话说明，并更新该节开头的技能数量。

- **`INSTALL.md` / `INSTALL.en.md`**：需要第三方 Python 依赖时，补到「4. Python 依赖」表。

### 1.2 状态标签

| 标签 | 含义 |
|-------|------|
| `Draft` | 规则已定义，但尚未在真实案例上测试 |
| `Beta` | 已在示例上测试，仍可能存在边界问题 |
| `Preview` | 新上线，已可使用，但触发方式和输出格式可能还会调整 |
| `Stable` | 已在真实学术内容上验证，规则相对稳定 |

---

## 二、SKILL.md 规范

### 2.1 Frontmatter

```yaml
---
name: <skill-name>              # 必须，与目录名一致
name_zh: 中文显示名              # 推荐
version: 1.0.0                  # 推荐
author: AMiner                  # 推荐
contact: report@aminer.cn       # 推荐
description: >                  # 必须
  [激活条件] 什么情况下应该激活此技能。
  [适用场景] 具体能做什么。
  [路由规则] 与其他技能的边界（何时不应使用此技能）。
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

`name` 和 `description` 必须填写；`version` / `author` / `contact` 推荐填写。

**`description` 的三段结构**：激活条件 → 适用场景 → 路由规则（何时转给其他技能），缺一不可。可参考现有技能的写法。

**`metadata.openclaw.requires`**：
- `bins`：脚本运行所需的可执行文件（如 `["python3"]`）
- `env`：运行时必须存在的环境变量，只填真正必须的（可选的模型 key 等在运行时动态检查的变量不用放这里）
- `primaryEnv`：主要的 key 名称

### 2.2 正文必须包含的内容

**环境变量检查**：执行任何操作前先检查必需的 key，统一使用以下格式：

```bash
[ -z "${AMINER_API_KEY+x}" ] && echo "AMINER_API_KEY missing" || echo "AMINER_API_KEY exists"
```

缺失时立即停止，引导用户到 https://open.aminer.cn 获取 Token。**任何输出中都不得打印 key 的值。**

**执行示例**：给出可直接运行的 bash 命令，参数尽量使用默认值，让读者一眼看懂怎么运行。

**运行约束**：列出模型或 Agent 执行时必须遵守的规则（如预算上限、不得伪造数据、计费提醒等）。

**File Map**（多文件技能）：简要说明每个文件的职责。

---

## 三、commands/<command-name>.md 规范

仅当技能提供 slash command 时需要。命令文件采用四段式结构：

```markdown
---
description: 一句话描述
argument-hint: [参数格式提示]
allowed-tools: Read, Bash, Glob, Grep
---

# /command-name — 标题

User invoked ... with the following arguments:
$ARGUMENTS

## Your task

### 1. Pre-flight
[检查环境变量和依赖，任何检查失败立即停止，不执行脚本]

### 2. Parse `$ARGUMENTS`
[说明从参数中提取哪些字段，以及默认值]

### 3. Run
[给出可直接复制的执行命令]

### 4. Present the result
[说明如何将输出展示给用户]
```

`Pre-flight` 至少要检查所有必需的环境变量；有 Python 脚本的还应检查依赖包是否已安装：

```bash
python3 - <<'PY'
import importlib.util
missing = [name for name in ("package1", "package2") if importlib.util.find_spec(name) is None]
print("Missing: " + ", ".join(missing) if missing else "Python dependencies exist")
PY
```

---

## 四、Python 代码规范

### 4.1 不要硬编码密钥或供应商信息

```python
# 错误：硬编码 key 或 base_url
client = OpenAI(api_key="sk-xxx", base_url="https://some-provider.com/v1")

# 正确：从环境变量读取，缺失时报错而不是静默失败
api_key = os.getenv("BIGMODEL_API_KEY")
if not api_key:
    raise ValueError("BIGMODEL_API_KEY is required. Set it in the environment or pass --api-key.")
```

`AMINER_API_KEY` 和模型 key 均不得出现在代码中，只能从环境变量或命令行参数读取。模型 key 使用带供应商前缀的变量名（如 `BIGMODEL_API_KEY`、`DEEPSEEK_API_KEY`、`GLM_API_KEY`）；base_url 和模型名可以有默认值，但必须能通过环境变量（如 `DEEPSEEK_BASE_URL`、`LLM_MODEL`）或参数覆盖。

### 4.2 多个模块共用的工具函数放到公共模块

多个脚本都需要同一批工具函数（如 ID 提取、结果去重、批量请求）时，提取成一个公共模块（如 `_utils.py`），各文件统一 import，不要各自复制一份：

```python
# 错误：在 module_a.py 和 module_b.py 里各写一份 dedupe_preserve_order

# 正确：写在 _utils.py，两处都 import
from _utils import dedupe_preserve_order, extract_paper_id
```

### 4.3 `__all__` 只列本模块定义的函数

模块从其他模块 import 的函数，不要写进本模块的 `__all__`：

```python
# 错误：normalize_paper_detail 实际定义在 _utils.py
__all__ = ["search_papers", "normalize_paper_detail"]

# 正确：只列本文件里真正定义的
__all__ = ["search_papers", "search_adding"]
```

### 4.4 不要保留无用的参数或字段

重构时如果某个参数在代码里从未使用，应一并删除，不要只改个默认值留着：

```python
# 错误：api_type 存了但从未读取
self.api_type = api_type

# 正确：直接删掉这个参数
```

### 4.5 requirements.txt 格式

使用 `>=` 指定最低版本，不要锁死到具体小版本：

```
openai>=1.68.0
requests>=2.31.0
```

---

## 五、PR 提交前自查清单

**文件完整性**
- [ ] `SKILL.md` 存在，frontmatter 的 `name` 与目录名一致，`description` 三段齐全
- [ ] 提供 slash command 的技能，`commands/<command-name>.md` 已创建
- [ ] `.claude-plugin/marketplace.json` 中已注册新技能（`source: ./skills/<skill-name>`）
- [ ] `SKILLS.md` 和 `SKILLS.en.md` 已更新（表格行、示例提示词、技能总数）
- [ ] `README.md` 和 `README.en.md` 已更新（skills 徽章、本地技能列表）
- [ ] 新增第三方依赖已写入 `requirements.txt` 和 `INSTALL.md` / `INSTALL.en.md` 的依赖表
- [ ] **必须**：`python3 tools/check_skills.py` 返回 0 个问题。该脚本检查：目录名与 `SKILL.md`、`SKILL.zh.md` 的 frontmatter `name` 一致；每个技能在 `marketplace.json` 中都有 `source` 为 `./skills/<skill-name>` 的条目；每个技能都出现在 `SKILLS.md` 和 `SKILLS.en.md` 的目录表中；`README.md` / `README.en.md` 的 skills 徽章数字与实际技能数一致

**SKILL.md 内容**
- [ ] `description` 包含激活条件、适用场景、路由规则
- [ ] 正文有环境变量检查的 bash 命令
- [ ] 正文有可运行的执行示例
- [ ] 没有硬编码的 key；base_url、模型名可通过环境变量或参数覆盖

**代码质量**
- [ ] 多文件共用的工具函数已提取到公共模块，没有跨文件复制
- [ ] `__all__` 只列本模块定义的函数
- [ ] 没有只存储、从未读取的参数或字段

**密钥安全**
- [ ] 代码、脚本、测试数据中没有任何密钥明文
- [ ] `SKILL.md` 和 `commands/` 文件中没有打印 key 值的指令

---

## 六、如何提交

1. Fork 本仓库，在 fork 中从最新的 `main` 拉一个分支（如 `feat/<skill-name>`、`fix/<skill-name>-xxx`）。
2. 完成修改并通过上面的自查清单。
3. 在 GitHub 上向 `AMinerOrg/aminer-research-skills` 的 `main` 分支发起 PR，说明改动内容和验证方式。

**PR 标题**使用 Conventional Commits 格式，scope 一般为技能名，描述中英文均可：

```
feat(sota-finder): 支持导出 CSV 榜单
fix(aminer-pdf-ocr): 修复大文件上传超时
docs(readme): 补充 Python 依赖说明
refactor(skills): 抽取公共请求函数
```

**其他要求**：
- 一个 PR 只做一件事，不相关的改动拆成多个 PR。
- 绝不提交 Token 或任何密钥。`.env`、`config.local.yaml` 等本地配置已在 `.gitignore` 中，不要强制添加；不要提交 `outputs/` 等运行产物。
- 草稿（Draft）PR 不会进入评审，准备好后请点 Ready for review。

---

## 七、PR 如何处理

本仓库的 PR **不通过 GitHub 的合并按钮合并**，而是走内部评审流程：

1. PR 提交后会被自动同步到内部评审流程，由维护者评审。
2. 之后向 PR 分支推送的更新会被自动同步，无需重新开 PR。
3. 改动被接受后，会以 **AMiner Bot** 署名的 commit 落到 `main`，并通过 `Co-authored-by` 标注你为共同作者，因此仍会计入你的 GitHub 贡献记录。
4. 随后 PR 会被自动关闭，并附上一条指向该 commit 的评论。PR 显示为 Closed 而非 Merged 属于正常情况。

**遇到机器人评论时**：
- **提示与 `main` 冲突**：将分支 rebase 到最新的 `main`，解决冲突后推送（`git push --force-with-lease`）。
- **提示改动涉及受限路径**：从 PR 中移除对这些路径的改动后重新推送。
