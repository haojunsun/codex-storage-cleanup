# Codex Storage Cleanup

先看清 Codex 的空间花在哪里，再决定清理什么。

这是一个用于 Codex 的 Agent Skill，附带只读 Python 扫描器。它会汇总目录占用、按真实会话 ID 合并历史文件，并列出需要人工核对的临时文件和备份候选。

**扫描器没有删除模式。** 清理操作由 Agent 在用户明确指定范围后，按 Skill 中的核对流程处理，不是一键删除工具。

## 能做什么

- 显示 Codex 根目录总量、主要目录和最大的 N 个聊天。
- 结合历史首条 `session_meta` 与索引核对聊天标题、项目归属和完整会话 ID。
- 识别 `.jsonl` 和 `.jsonl.zst`，将属于同一会话的文件合并计算。
- 列出插件升级暂存、部分临时副本和备份候选，候选不等于已经批准删除。
- 输出 JSON 或 Markdown；使用旧 JSON 报告比较目录净变化。
- 提供聊天删除、项目移除、数据库回收及压缩配置的检查流程，区分聊天数据与项目源代码。

## 安装

把以下指令发给支持安装 Skill 的 Codex：

```text
请从 https://github.com/haojunsun/codex-storage-cleanup 安装这个 Skill。
安装后只确认文件完整，不执行扫描或清理。
```

也可以从 GitHub 的 **Code → Download ZIP** 下载，将含有 `SKILL.md`、`agents/`、`references/`、`scripts/` 的整个文件夹放到你的 Codex Skills 目录，文件夹命名为 `codex-storage-cleanup`。常见目录为 `~/.codex/skills/`；以自己的 Codex 配置为准。

## 在 Codex 中使用

```text
使用 $codex-storage-cleanup，只读扫描 Codex 目录。
列出总占用、主要目录、最大的 20 个聊天及清理候选。
保留完整会话 ID 和文件路径。
先不要删除、压缩或修改配置。
```

看完清单再指定处理对象。聊天编号会随扫描结果变化，执行前应固定完整会话 ID，并重新核对标题、项目、关联文件及运行状态。

## 直接运行扫描器

需要 **Python 3.10+**。基础扫描使用标准库，不自动安装依赖。

在仓库根目录运行：

Windows PowerShell：

```powershell
python -X utf8 scripts/scan_codex.py --top 20 --format markdown
```

macOS：

```bash
python3 scripts/scan_codex.py --top 20 --format markdown
```

根目录优先级为 `--codex-home` → `CODEX_HOME` → `~/.codex`。扫描器默认输出 JSON 到标准输出，不自动保存报告。

自定义目录：

```powershell
python -X utf8 scripts/scan_codex.py --codex-home "D:\CodexData" --top 10
```

需要比较前后变化时，可在本机保存报告：

```powershell
python -X utf8 scripts/scan_codex.py --top 20 | Set-Content -LiteralPath before.json -Encoding utf8
# 完成另行明确授权的维护后，再比较
python -X utf8 scripts/scan_codex.py --top 20 --compare before.json --format markdown
```

`--compare` 只做比较，不执行清理。报告包含聊天标题、项目名称、会话 ID 和本地路径，分享前请自行检查并脱敏，不要直接提交真实报告。

## 压缩历史

对于 `.jsonl.zst`，优先使用 Python 的 `compression.zstd` 或已安装的 `zstandard`；也可使用具备内置 Zstandard 解码能力的 Node.js。可通过 `--node <可执行文件路径>` 指定 Node。

解码器只读取用于确认身份的首条记录，不全量解压历史。没有可用解码器时使用严格的索引路径匹配，无法核实的会话会标记为身份未核实，不应据此删除。扫描器不会为此自动安装依赖。

## 边界

- 大小按文件长度合计，不能等同于实际磁盘块占用或保证能释放的空间。
- 不跟随符号链接、Windows junction/reparse point。
- 不输出完整聊天正文、配置或认证文件内容；报告仍含标题和路径等元数据。
- 数据库以只读方式查询业务数据；SQLite 读取活跃 WAL 时可能维护共享内存协调文件。
- 归档不等于永久删除，开启压缩也不等于历史总量受到限制。
- 不直接删除 SQLite、WAL、SHM，不修改会话表，不剪裁 JSONL。
- `.tmp`、`skills`、插件缓存中可能仍有在用文件，不能只凭目录名称或文件年龄删除。
- 删除聊天不保证图片、附件、备份或项目侧栏入口一起消失。
- 删除接口、压缩开关和数据库维护条件以当前安装版本为准；遇到运行占用或安全拦截应报告阻碍。
- 本项目不是 OpenAI 官方清理工具。

## 测试

```powershell
python -B -X utf8 scripts/test_scan.py
```

测试使用系统临时目录中的合成数据，不扫描真实聊天。

2026-10-19 在 Windows 上运行 7 项测试，6 项通过，1 项因当前环境不允许普通用户创建符号链接而跳过。覆盖真实会话 ID 合并、索引与中文标题、无数据库时不创建、插件源候选区分、路径处理、Zstandard 首条记录和报告比较。macOS 包含路径处理与使用说明，尚无真实 Mac 验证结果。

## 文件

- `SKILL.md` — Agent 的扫描、清理和复查规则。
- `agents/openai.yaml` — Skill 显示信息和默认调用示例。
- `scripts/scan_codex.py` — 只读扫描器。
- `scripts/zstd_headers.cjs` — Node.js 压缩历史首条记录读取器。
- `scripts/test_scan.py` — 隔离测试。
- `references/cleanup.md` — 清理操作及验证条件。

## License

MIT，见 [LICENSE](LICENSE)。
