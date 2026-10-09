# 文档目录

[返回项目首页](../README.md)

## 按任务阅读

| 任务 | 从这里开始 | 看完能完成什么 |
| --- | --- | --- |
| 将 WAR / dist.zip 打包 | [Fast 打包](guides/FAST.md) | 选择 TongWeb/Tomcat 模板，首次配置，日常打包与交付 |
| 使用 Dockerfile | [Dockerfile 构建](guides/BUILD.md) | 选择基础镜像来源，检查与构建，设置输出和构建参数 |
| 设置内网地址和鉴权 | [统一配置](guides/CONFIGURATION.md) | 找到 config.json，配置仓库、模板、缓存与路径 |
| 查找命令 | [命令速查](guides/COMMANDS.md) | 按用途找到命令，区分镜像归档和 rootfs 归档 |
| 合并镜像或处理文件系统 | [rootfs 与扁平化](ROOTFS_COMMANDS.md) | 使用 export/import/--change/flatten |
| 使用高级构建能力 | [进阶构建](guides/ADVANCED.md) | 配置 RUN、平台、缓存、交付证明和封闭构建 |
| 处理失败或疑惑 | [常见问题](guides/TROUBLESHOOTING.md) | 根据报错找到原因和下一步操作 |

第一次使用：**[项目首页](../README.md) → Fast 或 Dockerfile 指南 → 配置说明**。平时查命令直接打开命令速查。示例地址、包路径和镜像标签都需要按实际环境替换。

## 功能参考

这些页面描述当前实现的细节和边界；使用步骤优先阅读上面的指南。

| 文档 | 内容 |
| --- | --- |
| [Dockerfile 兼容表](DOCKERFILE_COMPAT.md) | 指令、现代 flag、变量、链接与不支持的语法 |
| [本地 CAS 镜像库](CAS_STORE.md) | 镜像落盘、构建复用、来源冲突、转换与清理 |
| [构建进度输出](PROGRESS.md) | 普通终端、CI、JSON 事件和各阶段输出示例 |
| [配置模板](../config.example.json) | schemaVersion 2 的可编辑 JSON 示例 |

## 开发与维护

| 文档 | 内容 |
| --- | --- |
| [架构说明](ARCHITECTURE.md) | 模块分工、数据流、执行模型及实现边界 |
| [镜像身份契约](IMAGE_IDENTITY.md) | ImageID、manifest digest、原始 JSON 字节与转存规则 |

## 历史与审计

这里保留实现过程和当时的测试结论。早期示例可能已更新，日常操作以用户指南、兼容表和当前命令的 `--help` 为准。

- [阶段技术记录](PHASE_NOTES.md)
- [2026-09-29 审计](AUDIT_2026-09-29.md)
- [2026-09-30 审计](AUDIT_2026-09-30.md)
- [2026-10-09 审计与后续修复](AUDIT_2026-10-09.md)

## 文件放在哪里

```text
pyimagebuilder/
├── README.md                 项目入口与最短使用路径
├── main.py / fast.py / ...   程序与独立脚本
├── config.example.json      配置模板
├── config/                  TongWeb XML 等部署文件
├── docs/
│   ├── README.md             本文档目录
│   ├── guides/               按任务组织的使用指南
│   └── *.md                  兼容表、技术参考和历史记录
└── tests/                    回归与目标环境验收脚本
```

运行数据位置由配置决定：`data/image-store/`、`cache/layers/`、应用包和输出镜像不应随程序版本反复打包。已有参考文档保留原文件路径，便于旧链接继续使用。
