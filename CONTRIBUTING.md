# 开发与维护

[项目首页](README.md) · [文档目录](docs/README.md) · [架构说明](docs/ARCHITECTURE.md)

## 仓库包含什么

公开仓库保存能让其他人理解、使用和维护项目的内容。

| 内容 | 提交规则 |
| --- | --- |
| 核心 Python 模块、独立入口和 `progress/` | 保留，核心使用标准库，兼容目标为 Python 3.7 及以上 |
| `tests/` 与 Linux 验收脚本 | 保留，便于复现问题、防止修复后再次出错 |
| README、用户指南、兼容表、架构与镜像身份契约 | 保留，随功能变化更新 |
| `config.example.json`、`config/tongweb.xml` | 保留示例；地址、账户和部署路径需按实际环境修改 |
| 本机 `config.json`、密码、token、签名私钥 | 不提交；鉴权通过环境变量提供 |
| 镜像库、缓存、WAR、dist.zip、输出 tar、临时目录 | 不提交，属于运行数据或交付产物 |
| 审计日志、阶段记录和本机测试报告 | 在仓库外保存；稳定的使用约定和实现边界写入正式文档 |

`.gitignore` 是辅助措施，提交前仍要检查文件内容。示例配置使用占位地址和账户，不放真实凭据。

## 运行测试

在项目根目录执行，Windows 用 `python`，Linux 可使用 `python3`。

```text
python -m unittest discover -s tests -v
python conformance.py offline
```

单元测试会创建临时镜像和本地模拟服务，不要求内网仓库或 Docker。离线兼容性检查使用独立读取逻辑核对生成镜像，不能代替真实 Docker 对照。

运行针对某个文件的测试：

```text
python -m unittest discover -s tests -p test_image_identity_contract.py -v
```

完整回归建议使用独立的临时目录：启动 Python 前，将 Windows 的 `TEMP` / `TMP` 或 Linux 的 `TMPDIR` 指向新建的空目录，避免复用以前运行的缓存。不要并发执行缓存清理与构建。

## 哪些场景需要目标环境验收

| 场景 | 验收方法与条件 |
| --- | --- |
| 无 RUN 的构建和归档处理 | 单元测试、离线兼容样例；实际应用仍需启动检查 |
| Docker 语义与可加载性 | 有 Docker 的机器上运行 `python conformance.py reference`；具体项目使用 `compare --help` 查参数 |
| Linux RUN、whiteout、cache/secret mount | 在有 Linux 内核能力的构建机上运行 `conformance.py linux --help`，提供匹配平台的基础 tar、镜像引用及权限 |
| Registry / Artifactory 下载与鉴权 | 在目标内网用测试账户与测试仓库验证，避免覆盖正式标签 |
| amd64 / arm64 跨架构 RUN | 验证目标内核、QEMU/binfmt_misc 和基础镜像可执行文件 |
| Python 3.7 兼容性 | 已在 Windows/Python 3.7.9 运行完整回归；其他解释器版本和操作系统需分别验证，语法解析不能替代运行测试 |

最近完整回归在 Windows/Python 3.7.9 与 Python 3.12 各运行 387 项测试，各通过 384 项、跳过 3 项；跳过项涉及当前账户不能创建符号链接。真实 Docker、Linux RUN 和内网鉴权尚未在该环境验收。报告测试结果时，应同时说明解释器、操作系统、跳过项和外部依赖。

## 修改与提交

1. 先复现问题，明确失败输入和预期行为。涉及镜像身份时，遵守 [原始 JSON 与摘要契约](docs/IMAGE_IDENTITY.md)。
2. 修改实现，并为实际缺陷增加回归测试；CLI 或 Dockerfile 行为变化时同步更新相应指南和兼容表。
3. 先运行相关测试，涉及跨模块行为时再运行完整回归。说明未执行的目标环境验收。
4. 检查 `git diff` 和 `git status`，确认没有真实鉴权信息、业务包、运行数据或本机报告。
5. 提交说明写清修改原因、行为变化和验证范围。公共 docstring 与复杂逻辑说明沿用中文，解释约束和原因。

测试夹具优先在临时目录动态生成。不要为了一个回归用例提交大型镜像或真实业务文件。

## 仓库与交付包

GitHub 源码仓库保留测试，方便开发维护。面向内网使用者的精简交付包可以只带运行模块、配置示例和必要的用户文档，省去开发测试与内部记录。

如果省去 `tests/`，`conformance.py linux` 无法执行它引用的 Linux 验收脚本；需要该验收能力时，应使用完整源码仓库或包含测试的开发包。GitHub 自动生成的源码 ZIP 会包含仓库中的测试，不等同于精简运行包。
