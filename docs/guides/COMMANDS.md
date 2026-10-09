# 命令速查

[文档目录](../README.md) · [项目首页](../../README.md)

以下命令在工具目录执行。`IMAGE` 表示镜像引用，`archive.tar` 表示镜像归档，`rootfs.tar` 表示纯文件系统归档；它们不能互换。查完整参数用 **对应子命令的 `--help`**，例如 `python .\main.py flatten --help`。

## 1. 构建与快速打包

| 命令 | 作用 |
| --- | --- |
| `python main.py fast PACKAGE PROJECT VERSION --profile tongweb` | 按模板打包 WAR/dist.zip |
| `python main.py fast init --help` | 查看首次创建模板的参数 |
| `python main.py build CONTEXT -t IMAGE -o archive.tar` | 使用 Dockerfile 构建 |
| `python main.py build CONTEXT --check` | 静态检查，不下载或执行 RUN |

首次步骤：[Fast 指南](FAST.md) · [Dockerfile 构建](BUILD.md)。`main.py` 也接受省略 build 的旧式构建参数；新命令示例统一使用显式 build。

## 2. 查看与管理本地镜像

| 命令 | 作用 |
| --- | --- |
| `python main.py verify archive.tar` | 核对归档结构、摘要与层 |
| `python main.py inspect archive.tar` | 查看归档里的镜像配置 |
| `python main.py history archive.tar` | 查看镜像保存的构建历史 |
| `python main.py images` | 列出本工具镜像库的镜像 |
| `python main.py load -i archive.tar` | 导入已有 Docker 镜像归档到本工具镜像库 |
| `python main.py save IMAGE -o archive.tar` | 从本工具镜像库导出 Docker 归档 |
| `python main.py tag archive.tar company/app:2 -o retagged.tar` | 为归档改标签，写入新的归档 |
| `python main.py rmi IMAGE` | 删除对应的本地镜像引用及受管理 tar |
| `python main.py image prune --older-than 30 --dry-run` | 预览清理超过 30 天的镜像；去掉 dry-run 执行 |
| `python main.py system df` | 查看镜像库和指令缓存占用 |

`inspect/verify/history` 使用归档路径；`save/rmi` 使用已入库引用。归档有多个标签/平台时按命令支持选择 `--tag`、`--platform`；镜像库中不同来源同名时，使用 `--source local|registry|artifactory`。

**本工具 load/save 操作 PyImageBuilder 镜像库，不连接 Docker daemon。** 本工具 tag 的输入是归档文件，也不是原地修改 Docker 中的标签。image prune 按年龄清理，可以删除已打标签的镜像，应先查看预览。

## 3. 文件系统与扁平化

| 命令 | 作用 |
| --- | --- |
| `python main.py export archive.tar -o rootfs.tar` | 导出最终可见文件树，不带运行配置 |
| `python main.py import rootfs.tar company/app:1 -o image.tar --change 'CMD ["/app/start.sh"]'` | 从文件系统生成新镜像，并设置配置 |
| `python main.py flatten archive.tar -t company/app:flat -o flat.tar` | 合并为单层，默认保留运行配置 |

一般镜像交付用 **save**；希望现有镜像合并为单层用 **flatten**。export 的 rootfs.tar 不能直接 docker load。import 从空运行配置开始，flatten 则继承配置。完整参数、stdin/stdout 和 `--change` 支持范围见 [rootfs 指南](../ROOTFS_COMMANDS.md)。

## 4. 下载与推送

| 命令 | 作用 |
| --- | --- |
| `python main.py pull harbor.internal/project/base:1` | 从远端拉取并注册；显式 pull 会刷新标签 |
| `python main.py pull nmkf.zpk.abc/repo/base:1 --source artifactory` | 使用 Artifactory 下载路径 |
| `python main.py push archive.tar harbor.internal/project/app:1` | 推送镜像到标准 Registry/Harbor |
| `python main.py manifest inspect harbor.internal/project/app:1` | 查看远端 manifest/index |
| `python main.py manifest create --amd64 amd64.oci.tar --arm64 arm64.oci.tar -t company/app:1 -o multi.oci.tar` | 合并两个 OCI 变体为双平台 index |
| `python main.py manifest push multi.oci.tar harbor.internal/project/app:1` | 推送双平台 OCI 归档 |

推送默认拒绝覆盖已有远端标签；明确需要时加 `--overwrite`。账户、密码环境变量与 CA 由 [统一配置](CONFIGURATION.md) 管理。`pull -o archive.tar` 可以另存一份交付 tar。自动 build 拉取与显式 pull 的刷新语义见 [构建指南](BUILD.md)。

## 5. CAS、缓存与分析

| 命令 | 作用 |
| --- | --- |
| `python main.py cas inspect IMAGE` | 核对 CAS 引用及 blob |
| `python main.py cas import archive.oci.tar IMAGE --format oci` | 将受支持的单平台 OCI 归档导入 CAS |
| `python main.py cas export IMAGE -o archive.tar` | 从所选 CAS 引用生成 Docker 归档 |
| `python main.py cas prune --dry-run` | 预览回收无引用 blob |
| `python main.py cache ls` | 查看指令缓存 |
| `python main.py cache prune --older-than 30 --dry-run` | 预览清理过期指令缓存 |
| `python main.py analyze --archive archive.tar --output report.json` | 分析大层、重复文件和覆盖内容 |
| `python main.py optimize --archive archive.tar --output optimized.tar` | 在既有边界内保守删除冗余整层 |

`optimize` 与 `flatten` 不同：前者尝试删掉冗余整层，后者重写最终文件树为一层。清理镜像库的孤立 blob 前应停止同一镜像库上的构建、拉取、导入等操作；详细行为见 [CAS 镜像库](../CAS_STORE.md)。

## 6. 独立脚本

统一入口之外，原有独立运行能力保留。常用入口：

| 独立入口 | 用途 |
| --- | --- |
| `python fast.py ...` | 参数与 main.py fast 后面的参数一致 |
| `python rootfs_archive.py export/import/flatten ...` | 文件系统处理和扁平化 |
| `python image_cli.py --help` | 镜像检查、存储、缓存和 Registry 管理命令 |
| `python registry.py --help` | 独立标准 Registry 客户端 |
| `python artifactory_download.py --help` | 独立 Artifactory 下载器 |
| `python optimizer.py --help` | 分析、优化与缓存审计 |
| `python attest.py --help` | 生成签名密钥、核对交付证明 |
| `python hermetic.py --help` | 生成严格构建输入锁 |
| `python conformance.py --help` | 离线及目标环境兼容性验收 |

当前 main.py 不提供 Docker 的容器生命周期命令，如 create/run/start/stop/rm；没有 bundle export/import、login/logout 命令。Docker CLI 与本工具有相似的操作名称，具体输入、输出和作用位置以上述说明为准。
