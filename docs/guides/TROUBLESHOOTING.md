# 常见问题

[文档目录](../README.md) · [命令速查](COMMANDS.md) · [统一配置](CONFIGURATION.md)

## 根据现象处理

| 现象 | 先核对什么 | 下一步 |
| --- | --- | --- |
| Fast profile already exists | 同名模板已经配置 | 编辑 config.json；或者用另一名称创建新 profile |
| Unknown fast profile / 配置找不到 | 配置发现顺序、profile 名称 | 显式传 `--config` 和 `--profile` |
| 基础 tar 不包含 FROM 标签 | baseImage 与 tar 内标签不同 | `inspect` 查看归档，使用真实标签 |
| 本地找不到 FROM | 镜像库、平台、来源、offline | 拉取或 load 所需镜像；对齐 `--config`/`--image-store` |
| 下载 401/403 | 账号、passwordEnv、当前终端变量 | 设置正确密码变量，确认仓库权限 |
| HTTPS 证书失败 | 企业 CA 文件及证书链 | 配置 caFile / 使用命令支持的 --ca-file |
| COPY --chown 无法解析账户 | 基础镜像里的 passwd/group | 使用真实账户或合适的数字 UID:GID，修改模板 |
| .dockerignore 报不支持 | 存在实际忽略规则 | 整理专用构建上下文，按兼容表使用 --exclude |
| Output already exists | 输出路径已有文件 | 选择新文件名；核对旧结果后再自行处理 |
| Image reference is busy | 同一引用正被另一进程操作 | 等当前操作结束后重试；不要删除 lock 文件 |
| Start Python with PYTHONHASHSEED=0 | 严格封闭模式没有固定 Python 哈希种子 | 在启动 Python 前设置 `$env:PYTHONHASHSEED = '0'`，再生成锁/构建 |
| RUN 在 Windows 失败 | 未进入 Linux 或内核能力不足 | 使用已配置 WSL，按进阶指南验收权限与 OverlayFS |
| arm64 RUN 出现执行格式错误 | 宿主架构与镜像不同 | 配置 QEMU/binfmt_misc，或在同架构 Linux 上构建 |
| docker load 不接受 rootfs.tar | 文件只是文件系统归档 | 先用 import 生成镜像；保留原启动配置时优先 flatten |
| history 没有记录 | 原镜像未保存 history | 查看 available / 未记录层数量；不据此判定归档损坏 |

build 出错需要更多上下文时加 `--debug`；持续集成建议 `--progress plain`。其他入口先查看其 `--help`，并非所有子命令都有同样参数。

## 为什么改了配置却没生效

当前目录可能有另一份 config.json，或 `PYIMAGEBUILDER_CONFIG` 指向别处。使用显式 `--config` 确认文件，并核对相对路径的基准：配置文件目录，而不是 main.py 目录或包所在目录。

Fast 中的 profile 名是模板选择名；版本仍来自命令行位置参数。修改 defaultProfile 不影响显式 `--profile tomcat`。

## 下载后的基础镜像放在哪里

使用 `cache.imageStore` 或命令支持的 `--image-store`。有配置但未指定时，默认位于配置目录的 `data/image-store/`；无配置时使用系统临时目录的默认镜像库。

普通 build/Fast 直接读取 CAS 中的 config 和层；远端压缩层仍需解压并校验 DiffID，解码结果可以复用。显式 pull/save 会生成 Docker tar，所以库里可能同时有压缩 blob、未压缩层和 tar，磁盘占用高于下载量。完整机制见 [CAS 镜像库](../CAS_STORE.md)。

## 为什么已经构建成功，images 里没有它

build/fast 输出归档，不自动注册镜像库。执行 `python main.py load -i app.tar` 后可按标签查库。`images` 也不会查看目标 Docker 的本地镜像列表。

## 为什么缓存命中了还会耗时

命中不代表没有 I/O。读取与校验基础镜像、检查缓存输入、写出完整归档及最终校验仍需时间。`--verify-cache` 会增加完整内容读取；`--pull` 会刷新远端层。先参考 [分阶段进度输出](../PROGRESS.md)，确认耗时位置，再决定是否调整下载并发或数据盘。

## 怎么安全清理

1. `system df` 看镜像库和缓存占用。
2. `cache prune --older-than 30 --dry-run` 预览指令缓存清理。
3. `image prune --older-than 30 --dry-run` 预览镜像清理，它可能包含已打标签的镜像。
4. 停止同库上的构建、导入和拉取后，用 `cas prune --dry-run` 预览无引用 blob 回收。
5. 核对候选内容，再运行去掉 `--dry-run` 的对应命令。

各命令要使用同一配置或相同缓存路径。`rmi` 删除引用后不自动回收所有 blob；其他镜像共享的层也应保留。

## 扁平化、压缩和改标签会不会改变镜像 ID

仅转存、改标签应保留原始配置字节，保持 ImageID。重新构建、rootfs import、flatten 会生成派生镜像，ImageID 变化是预期行为。manifest digest、层 DiffID 和整个 tar 文件摘要是不同概念；详情见 [镜像身份契约](../IMAGE_IDENTITY.md)。

flatten 合并层，不保证体积一定变小。需要分析重复内容用 analyze，尝试删除冗余整层用 optimize，压缩归档也不能代替这些操作。

## 支持 Python 3.7 是否已实测

兼容目标是 Python 3.7；核心使用标准库，版本差异由 compat.py 处理。本机验证包含 3.7 语法检查，但实际运行使用较新 Python，不能据此宣称所有 3.7 环境已验收。真实 Linux RUN、WSL、Registry 鉴权和最终应用也需按部署环境测试。
