# 项目协作规范与准则

## Git 自动提交与推送规范
- **自动提交与推送规则**：每次完成代码修改、问题修复或需求实现并通过验证后，必须自动将变更提交并推送到远端仓库，无需等待用户额外提醒。
- **提交与推送流程**：
  1. 检查并添加修改文件：`git add <相关文件>`
  2. 编写规范清晰的提交信息并提交：`git commit -m "<type>: <description>"`
  3. 双向同步推送到所有已配置的远端源（GitHub `origin` 和 Gitee `gitee`）：
     - `git push origin <branch>`
     - `git push gitee <branch>`
