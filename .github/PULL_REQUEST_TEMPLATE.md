<!--
PR title must follow Conventional Commits / PR 标题必须符合 Conventional Commits：
  feat(<skill-name>): <what changed>     新增或修改技能能力
  fix(<skill-name>): <what changed>      修复技能问题
  docs: / refactor: / chore: ...         仓库级改动
The PR title becomes the commit message on main. / PR 标题会成为 main 上的提交信息。
See CONTRIBUTING.en.md / 详见 CONTRIBUTING.md。
-->

## What / 改动内容

<!-- What does this PR change, and why? / 这个 PR 改了什么，为什么？ -->

## Type / 类型

- [ ] New skill / 新增技能
- [ ] Change to an existing skill / 修改已有技能：<!-- skill name / 技能名 -->
- [ ] Repository-level (docs, tools) / 仓库级改动（文档、工具）

## Self-check / 自查清单

- [ ] `python3 tools/check_skills.py` reports 0 problems / 返回 0 个问题
- [ ] New skills are registered in `marketplace.json`, `SKILLS.md` / `SKILLS.en.md` and the README badge / 新技能已登记到 `marketplace.json`、`SKILLS.md` / `SKILLS.en.md` 和 README 徽章
- [ ] `SKILL.md` and `SKILL.zh.md` (if present) are in sync / `SKILL.md` 与 `SKILL.zh.md`（如有）内容一致
- [ ] New dependencies are listed in `requirements.txt` and `INSTALL.md` / 新依赖已写入 `requirements.txt` 和 `INSTALL.md`
- [ ] No tokens, keys or machine-specific paths / 不含 Token、密钥或本机路径
- [ ] I ran the skill's example command (or explain below why not) / 已实际运行技能的示例命令（未运行请在下方说明原因）

## Verification / 验证方式

<!-- Commands run and results. / 运行了哪些命令、结果如何。 -->
