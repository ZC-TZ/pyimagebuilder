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

## 浏览器能打开，但仓库诊断提示 TLS 证书校验失败

先从浏览器复制完整地址核对 `http://` / `https://`，不要只根据地址栏的“不安全”图标判断。
工作台诊断默认使用 HTTPS；如果实际服务是 HTTP，需要在现有仓库项中加 `"insecureHttp": true`，
并确认 `source` 为实际的仓库类型。Fast 模板的 `baseUrl` 也应使用实际协议；只改模板不会改变仓库诊断的协议。
保留已有 username/passwordEnv 等字段，保存配置并重启 Web 后再检查。

HTTPS 服务应保持证书校验，配置企业 CA 的 `caFile`，并核对证书主机名和有效期。
`insecureHttp` 切换为 HTTP，不会让不受信任的 HTTPS 证书被接受。

## Invalid layer permission mode: 16877

`16877` 是八进制 `040755`，包含目录类型位和 `0755` 权限。旧版把完整 st_mode 当成纯权限而拒绝，可能在基础镜像已成功下载后，于 FROM 失败。

更新代码后，匹配 tar typeflag 的类型位仅在内存中去除，保留 setuid/setgid/sticky；原始层、DiffID 和基础镜像配置字节保持不变。类型冲突、未知高位、负数和非整数仍拒绝。不要修改基础镜像、把权限改成 777 或删除镜像库；保持原 config.json 与 data/image-store，再执行原打包命令即可复用已下载镜像。

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

如果下载完成后，`应用基础层` 长时间停留在某几个层，需区分文件数量与层大小。
旧实现每新增或覆盖一个普通文件都会遍历已索引的整个文件树，小文件多时会出现平方级增长。
现在利用索引已补齐目录祖先的约束：不存在的路径直接跳过，普通文件、链接和特殊节点直接删除；
替换目录或应用目录 opaque 标记时仍清理对应后代，保留 whiteout 和硬链接语义。
这项修改适用于普通构建和 Fast，无需新参数或清空镜像库。已有正常 CAS 层可继续复用。

本地 Python 3.12 对照样例：基础层含 16,000 个 64 字节文件，索引从 6.86 秒降到
0.24 秒；同一 CAS 基础镜像的完整 Fast WAR 构建，首次指令缓存未命中从 7.90 秒降到
1.01 秒，再次命中从 7.30 秒降到 0.65 秒。四次输出的 SHA-256 相同。
该样例没有远端下载，也不代表真实大镜像、UOS 磁盘或内网的整体提速比例。

下载、解压、校验和索引是不同阶段；索引变快不会减少首次下载的字节量。
对比同一基础镜像更新前后的 `应用基础层` 耗时，并单独比较首次下载与本地复用构建。
更新代码后重启 Web 工作台，正在执行的旧进程不会自动换用新实现。

多阶段 `COPY --from` 和 SBOM 文件清单曾逐文件重开、重扫来源 tar。现在由同次操作的
`LayerReader` 复用成员索引和最多 8 个层句柄；Python 包检测也共用该读取器。
LRU 淘汰后再次访问同一层会重新扫描，操作结束时关闭全部句柄；不跨构建保存这份索引。
硬链接读取其固定的原始内容成员，最终 inode 元数据与内容头仍分别处理。

Docker/OCI 写出、COPY/ADD 数据复制、rootfs 导出和 Artifactory 归档打包采用
256 KiB 的 tar 复制块，减少默认 16 KiB 小块的读写与进度包装调用。
本地 128 MiB 单层导出对照的三次中位数：Docker 从 0.148 秒到 0.060 秒，
OCI 从 0.149 秒到 0.061 秒，归档字节摘要相同。实际磁盘收益需在目标机器测量。

800 个小文件的多阶段 COPY 对照从 5.99 秒到 0.038 秒，SBOM 从 5.96 秒到 0.045 秒；
同规模完整双阶段构建并生成 SBOM，从 13.41 秒到 1.54 秒，tar 和 SBOM 摘要均一致。
这些是无下载的本地样例；没有多阶段 COPY 或 SBOM 的 Fast 构建不能套用这组提速比例。
层摘要、tar 结构与最终交付摘要检查继续执行，已有镜像库和指令缓存无需清理。

## 跨盘导出为什么慢

系统临时目录和输出目录在不同文件系统时，旧路径需要额外读取并复制一次完整 tar。
现在可在输出盘的私有目录写出、fsync 和验证，再以硬链接排他发布，减少整包搬运及临时空间。
显式 `--workspace`、输出位于上下文内、暂存不可用或硬链接不支持时沿用原位置。

本地 C:→D: 的约 129 MiB 样例，构建 API 的三次中位数从 0.478 秒降到 0.354 秒，发布阶段完整复制量从
129 MiB 降到 0，六次输出 SHA-256 一致；该对照包含 fsync，不含 CLI 最终整包摘要和台账写入。
真实 UOS、机械硬盘和网络盘需重新测量。
Fast 和普通构建都可用 `--workspace PATH` 固定工作盘。更新后查看 `Publish Docker/OCI archive`
与 `Hash final output archive`，区分发布搬运和最终摘要的耗时，不能把它们都归因于下载。

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

兼容目标是 Python 3.7；核心使用标准库，版本差异由 compat.py 处理。本地自动测试已分别使用 Python 3.7.9 和 3.12 运行；这不能据此宣称所有 3.7 环境已验收。真实 Linux RUN、WSL、Registry 鉴权和最终应用也需按部署环境测试。


## 特殊路径为何被拒绝

RUN 挂载和 Fast 部署目录不允许解析到根目录；归档成员不允许父跳转或反斜杠。Windows 解压还会拒绝盘符、ADS、设备名、尾部点/空格和大小写冲突。出现路径错误时按 [路径处理规则](../PATH_POLICY.md) 检查输入，不要删除检查后重试。
