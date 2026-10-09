# PyImageBuilder

**在没有 Docker、Podman 的机器上，用 Python 将 WAR、dist.zip 或 Dockerfile 构建成镜像归档。** 默认生成可供 `docker load` 使用的 `.tar`，也支持 OCI 格式。

## 从哪里开始

| 你要做什么 | 使用方式 | 详细指南 |
| --- | --- | --- |
| 快速打包 WAR 或前端 dist.zip | `fast`，首次配置一次模板 | [Fast 打包](docs/guides/FAST.md) |
| 使用自己的 Dockerfile | `build`，支持本地基础镜像与自动拉取 | [Dockerfile 构建](docs/guides/BUILD.md) |
| 将已有镜像合并为单层，保留启动配置 | `flatten` | [文件系统与扁平化](docs/ROOTFS_COMMANDS.md) |
| 下载、查看、导入、导出或推送镜像 | 镜像与 Registry 命令 | [命令速查](docs/guides/COMMANDS.md) |

首次阅读建议按 **本页 → 对应使用指南 → 配置说明** 的顺序。全部文档见 [文档目录](docs/README.md)。

## 环境要求

| 构建场景 | 条件 |
| --- | --- |
| Fast、无 RUN 的 Dockerfile、镜像归档处理 | Windows 或 Linux，Python 3.7 及以上；核心使用标准库 |
| Dockerfile 中实际执行 RUN | Linux，或 Windows 上已安装的 WSL；还需 namespace、OverlayFS 和相应权限 |
| 离线构建 | 已准备本地基础 tar，或镜像库已有需要的镜像 |
| 加载并启动最终镜像 | 在目标机器使用 Docker 等兼容运行环境 |

**只有 Python 可以完成归档构建；执行 Linux RUN 还需要 Linux 内核能力。** 本项目是实验性构建器，支持范围见 [Dockerfile 兼容表](docs/DOCKERFILE_COMPAT.md)。Python 3.7 是兼容目标，本机未执行真实 3.7 运行时验收。

以下命令在工具目录执行，使用 PowerShell。`D:\packages`、`D:\images` 和内网地址都是示例，请替换为实际路径和地址。输出建议放在构建目录外；已有同名输出会报错。

## 快速开始：WAR 转镜像

### 1. 首次准备模板

已有基础镜像 Docker save tar 时，创建 TongWeb profile：

```powershell
python .\main.py fast init --base-tar D:\images\tongweb-base.tar `
  --base-image company/tongweb:11 --flavor tongweb --profile tongweb `
  --server-config .\config\tongweb.xml --output-dir D:\images
```

`--base-image` 必须是基础 tar 内真实的标签。命令会创建或向 `config.json` 添加 profile；**同名 profile 已存在时拒绝覆盖**，请编辑配置。没有基础 tar 时，使用 [从内网地址配置模板](docs/guides/FAST.md#从-artifactory-地址开始)。

### 2. 日常打包

```powershell
python .\main.py fast D:\packages\report-1.2.3.war sfcx 1.3.4 --profile tongweb
```

得到 `D:\images\sfcx_back-1.3.4.tar`，镜像标签为 `sfcx_back:1.3.4`。项目名和版本来自命令行，包文件名不会决定标签。

前端包与独立脚本也能使用：

```powershell
python .\main.py fast D:\packages\dist.zip sfcx 1.3.4 --profile tongweb
python .\fast.py D:\packages\report-1.2.3.war sfcx 1.3.4 --profile tongweb
```

前端默认标签为 `sfcx_front:1.3.4`。Fast 会生成临时 Dockerfile，并用 `COPY` 添加应用文件；不执行 `RUN`。模板差异、输出位置和完整流程见 [Fast 指南](docs/guides/FAST.md)。

## 使用 Dockerfile

```powershell
python .\main.py build D:\project --check
python .\main.py build D:\project --base-tar D:\images\base.tar `
  -t sfcx_back:1.3.4 -o D:\images\sfcx_back-1.3.4.tar
```

不提供 `--base-tar` 或 `--base-map` 时，构建会查找本地镜像库，缺失后尝试拉取 `FROM`。自动拉取需完整镜像引用及正确的仓库配置；`--offline` 禁止联网，`--pull` 刷新远端基础镜像。`--check` 仅检查静态输入，不下载镜像、不执行命令。

多阶段、平台、OCI 和 RUN 用法见 [构建指南](docs/guides/BUILD.md)。

## 验证和交付

在构建机器上检查归档：

```powershell
python .\main.py verify D:\images\sfcx_back-1.3.4.tar
```

将归档复制到有 Docker 的目标机器，再加载：

```text
docker load -i sfcx_back-1.3.4.tar
```

归档校验通过后，还需在目标环境启动应用，检查部署路径、启动配置和服务行为。

## 下一步

- [统一配置](docs/guides/CONFIGURATION.md)：下载地址、鉴权、TongWeb/Tomcat 模板、缓存路径。
- [命令速查](docs/guides/COMMANDS.md)：按用途查命令，以及与 Docker CLI 的差异。
- [进阶构建](docs/guides/ADVANCED.md)：RUN/WSL、跨架构、缓存、签名与严格封闭构建。
- [常见问题](docs/guides/TROUBLESHOOTING.md)：下载失败、找不到基础镜像、输出与缓存问题。
- [文档目录](docs/README.md)：用户指南、技术参考和审计历史的完整入口。
