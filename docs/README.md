# 文档目录

[项目首页](../README.md) · [命令速查](guides/COMMANDS.md) · [常见问题](guides/TROUBLESHOOTING.md)

第一次使用：先选一种入口完成一次构建，再根据需要查配置或技术参考。日常使用不必通读底层实现说明。

## 使用指南：按任务查

| 我现在要做什么 | 阅读入口 | 主要内容 |
| --- | --- | --- |
| 把 WAR / dist.zip 变成 tar | [Fast 打包](guides/FAST.md) | TongWeb/Tomcat、首次配置、UOS/Windows 命令、标签与部署路径 |
| 用浏览器操作工具 | [Web 工作台](guides/WEB.md) | 启动密码、选文件、构建、日志、交付、模板与清理 |
| 使用自己的 Dockerfile | [Dockerfile 构建](guides/BUILD.md) | 本地/远端基础镜像、检查、多阶段、平台和输出格式 |
| 设置地址、鉴权和路径 | [统一配置](guides/CONFIGURATION.md) | 配置查找、字段、HTTP/HTTPS、模板、数据位置 |
| 查打包版本与备份数据 | [打包台账与运行数据](guides/HISTORY.md) | builds、任务摘要、文件保存位置、迁移与下载 |
| 查一个具体命令 | [命令速查](guides/COMMANDS.md) | main.py 子命令、独立脚本和 Docker CLI 差异 |
| 排查报错 | [常见问题](guides/TROUBLESHOOTING.md) | 按现象定位和处理 |
| 定位打包为什么慢 | [性能排查](guides/PERFORMANCE.md) | 首次下载/本地复用、并发进度、磁盘与缓存耗时 |
| 使用 RUN、WSL、签名或封闭构建 | [进阶构建](guides/ADVANCED.md) | 环境要求、参数和验收边界 |
| 导入/导出 rootfs 或合并为一层 | [rootfs 与扁平化](ROOTFS_COMMANDS.md) | export、import、--change、flatten 与运行配置 |

## 功能参考：查支持范围和行为

| 文档 | 回答的问题 |
| --- | --- |
| [Dockerfile 兼容表](DOCKERFILE_COMPAT.md) | 哪些指令和 flag 有实际语义，哪些形式会拒绝 |
| [本地 CAS 镜像库](CAS_STORE.md) | FROM 如何落盘、复用、转换、处理来源冲突与清理 |
| [缓存与复用规则](CACHE_POLICY.md) | 缓存键包含什么，如何验证，旧缓存和异常如何处理 |
| [构建进度输出](PROGRESS.md) | CLI/TTY/Web/JSON 怎么展示，层事件和耗时怎么解释 |
| [配置模板](../config.example.json) | 可编辑的 schemaVersion 2 JSON 示例 |

## 开发参考：查实现契约

| 文档 | 内容 |
| --- | --- |
| [架构说明](ARCHITECTURE.md) | 模块分工、数据流、阶段、执行与输出模型 |
| [镜像身份契约](IMAGE_IDENTITY.md) | ImageID、manifest digest、原始 JSON 与转存边界 |
| [路径处理规则](PATH_POLICY.md) | 镜像路径、宿主路径、Windows 名称、链接和输出边界 |
| [镜像权限规则](PERMISSION_POLICY.md) | UID/GID、mode、xattr、硬链接、RUN 与缓存 |
| [并发与资源生命周期](CONCURRENCY_AND_RESOURCES.md) | 锁、取消、线程/进程、上传清理与鉴权边界 |
| [开发与维护](../CONTRIBUTING.md) | 测试命令、当前验证范围、目标环境验收和提交约定 |

## 哪份文档负责什么

操作步骤放在 `guides/`；支持范围与稳定行为放在功能参考；底层不变量放在开发参考。具体参数以当前子命令的 `--help` 为准，指南中的命令必须与它一致。

文档描述当前行为，不把开发对话、修复过程和旧基准记录混进操作步骤。一次功能变动应更新它所属的指南与必要参考，再检查首页和本目录的入口。

根目录保留 `README.md` 与 `CONTRIBUTING.md`。运行数据的位置由配置决定，镜像、缓存、台账和交付物不放进源码更新包；位置表见[运行数据](guides/HISTORY.md#3-数据保存在哪里)。
