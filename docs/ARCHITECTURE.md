# 架构与实现边界

[文档目录](README.md) · [项目首页](../README.md)

本页面向维护者，说明模块分工、数据流和实现边界。使用步骤见 [用户指南](README.md#按任务阅读)，Dockerfile 支持范围见 [兼容表](DOCKERFILE_COMPAT.md)，测试方法见 [开发与维护](../CONTRIBUTING.md)。

`main.py` 编排构建与镜像管理，Fast 复用同一构建器。基础镜像来自 Docker archive 或本地 CAS；Registry/Artifactory 下载器负责远端输入。文件指令生成层，元数据指令更新配置；RUN 另外使用 Linux 执行器。Docker/OCI writer 生成并校验交付归档。

Windows 上无 RUN 的构建原生执行；有 RUN 时可将源码和输入交给已存在的 WSL 环境。WSL 只切换执行地点，内核、权限与 OverlayFS 条件仍需满足。各模块均不调用现成镜像构建器或容器运行时。


## 阅读导航

| 主题 | 对应章节 |
| --- | --- |
| 整体流程 | [统一配置](#统一配置) · [数据流](#数据流) · [工程目录](#工程目录) |
| Dockerfile 构建 | [文件指令](#文件指令) · [多阶段状态](#多阶段状态) · [ARG 与挂载](#arg-与临时-run-挂载) |
| 存储与交付 | [层缓存](#层缓存) · [双格式输出](#双格式输出) · [Docker save 归档](#docker-save-归档) |
| 平台与执行 | [平台传播](#平台传播与双平台归档) · [RUN 隔离](#run-隔离) · [执行模型](#run-执行模型) |
| 质量与维护 | [可重复性](#可重复性约束) · [交付证明](#交付证明) · [独立语义验收](#独立语义验收) · [注释约定](#源码注释约定) |

## 源码注释约定

源码的模块、类、函数 docstring 和行内说明统一使用中文；`Registry`、`CAS`、`DiffID` 等协议术语，以及参数、字段、路径和命令名保留原写法，方便与代码和规范对应。

- 简单接口用一句话说明职责；复杂接口采用 Google 风格的 `Args`、`Returns`、`Raises` 段落，说明关键输入、实际返回类型及失败条件。
- 注释优先解释原因和边界：缓存依赖哪些输入、摘要在哪一步校验、何时发布 ref、失败如何恢复、临时挂载是否进入 layer。
- 行内注释放在 whiteout、硬链接、路径边界、并发顺序和跨文件系统发布等不直观的位置，不逐行复述循环、赋值或简单判断。
- 声明“已验证”时写清验证对象：`has_ref` 只是存在检查，压缩 blob digest 也不是解压后 DiffID。RUN 的执行与差异层生成分别由执行器和 OverlayFS 负责。
- 注释描述当前行为，避免“本阶段暂不支持”“以后接入”等已过时表述。功能变更时同步更新相关 docstring 和兼容表。

注释维护可通过去除 docstring 后的 AST 比较确认可执行代码未变化。部分 CLI 使用模块 `__doc__` 作为帮助摘要，因此中文化模块说明也会更新相应帮助摘要。

## 统一配置

`settings.py` 读取 `config.json`（schemaVersion 2），把相对缓存和 CA 路径解析到配置文件目录。`repositories` 按主机名匹配普通 `build/pull`、独立 Registry 客户端、Artifactory 下载器和 Fast 的基础镜像；命令行选项覆盖相应字段，密码始终通过配置指定的环境变量名在运行时读取。`fast.profiles` 存放多个应用打包模板，`fast init` 只增加一个 profile 并保留其他配置段。配置不参与镜像内容，缓存键仍以实际构建输入计算。

## 数据流

```text
Dockerfile ───────────────► DockerfileParser ───────┐
build context ────────────► LayerBuilder (COPY/ADD) ─┤
base-image.tar ───────────► ImageArchiveReader ─────┤
CAS blobs/ref ────────────► CASStore.open_base ──────┤
                                 │                   ▼
                            RootFSIndex       ImageBuilder
                         (按层应用 whiteout)         │
                                                     ▼
                                           ImageConfig + ImageArchiveWriter
                                                     │
                                                     ▼
                                              output-image.tar
```

本地镜像库由 `cas_store.py` 保存 content-addressed blob 与镜像引用。普通构建和 Fast 直接从 CAS 读取基础 config 和 layer；显式 tar 输入仍由 `ImageArchiveReader` 读取。`pull`/`save` 可以为需要 `docker load` 的场景生成 Docker tar；详见 [CAS 镜像库](CAS_STORE.md)。

`rootfs_archive.py` 为 export/import/flatten 提供独立入口，并由 main.py 分派。归档输入复用既有验证器；引用输入持 CAS 引用锁直接读取层。RootFSIndex 应用 whiteout 后，合并器只为最终可见 inode 预读 TarInfo，以偏移流式读取内容；最多保留 8 个 tar 句柄，不因逐文件查找或句柄淘汰重新扫描整层。RootFSIndex 也保存特殊文件的来源，合并器保留设备号/FIFO/PAX 元数据而不物化宿主文件系统。`image_changes.py` 复用 Dockerfile 解析与展开，只更新允许的运行配置；WORKDIR/VOLUME 不调用构建目录层逻辑。导入和扁平化显式使用 write_new，生成新的单层 history/配置身份，并经 writer.verify 后排他发布。export 的二进制 stdout 与进度 stderr 分开，import 可以从 stdin 落到私有工作区。详见 [rootfs 与扁平化](ROOTFS_COMMANDS.md)。

`store_lock.py` 用进程内 RLock 和内核文件锁保护同一引用：Windows 使用 msvcrt 字节锁，POSIX 使用 flock。CAS 引用锁按来源、引用及平台计算键，支持同线程跨 CASStore 对象的嵌套调用；竞争时立即报 busy。pull/load 的锁覆盖快照、下载或复制、tar/ref 发布和失败恢复；受管理归档的按需生成也持锁。rmi 在锁内先删除 ref，再删除 tar，tar 删除失败则恢复 ref；不会隐式执行全库 GC。CAS 发布在锁内重新检查 --replace 冲突，不能仅靠原子替换来保证“不覆盖”。锁文件保持稳定，不在释放时删除，进程退出后内核锁自然释放。现有全局 prune 仍要求避开其他存储操作。

CAS resolve 与 GC 共用引用内容校验。GC 会核对保留 ref 的身份和 manifest/config 摘要，以及层列表、大小、media type、平台和 DiffID 的一致性，避免 ref 遗漏层时把有效内容误判为孤儿；所有保留引用通过检查后才删除 blob。GC 只检查层存在性和大小，完整传输层 SHA-256 仍由 resolve 默认执行，避免清理时重复扫描全部大层。

`RootFSIndex` 在内存中还原**逻辑可见文件树**，不把不可信的 tar 成员直接释放到宿主文件系统。应用前检查整层声明的祖先类型，拒绝普通成员的同层非目录父路径与子路径冲突；同时预检所有 whiteout 的空普通文件形态、目标和保留名称，避免先删除再报错。删除只针对下层，不用本层替换后的父路径类型判断删除标记；允许删掉下层目录内容后把目录替换成文件。物化时删除路径不穿过下层符号链接，避免虚拟索引保留、实际文件却被误删。物化器复用预检，独立 conformance 读取器另行实现同等检查。它用于判断目标路径是否已经是目录，按层验证 whiteout，并从可见的 `/etc/passwd`、`/etc/group` 解析 `--chown` 名称。已有 base layer 的原始 tar 字节不变；每条 `COPY`、`ADD` 或创建目录的 `WORKDIR` 新增一个 layer。

WORKDIR 按当前 USER 创建缺失目录，保留已有父目录的属主。具名用户省略组时使用镜像 passwd 的主组，数字 UID 省略组时使用同值 GID；同一解析规则用于 --chown，不读取宿主账户。执行 RUN 前，RootFSMaterializer 按依赖顺序创建硬链接链，目标为本层尚未创建的链接时不能复用下层同名文件；循环依赖明确失败。硬链接目标的前导 `/` 解释为镜像根，相对路径仍拒绝 `..`，不会映射到宿主根目录。

`image_identity.py` 为权限参数及查询结果共用非负 UID/GID 范围检查。RUN 账户查询保留文件顺序，取首个匹配账户及它自身的附加组；显式组覆盖主组并关闭附加组加载。默认 UID 0 也需设置镜像身份，不能继承宿主 GID/附加组。RUN 物化前逐层预检文件 UID/GID，合法 Linux 属主范围为 0..4294967294，拒绝负数、4294967295 哨兵以及越界值；它与执行 USER 的 0..2147483647 范围分开，不降低合法文件属主的范围。RootFSMaterializer 自动补建的目录采用明确的 0:0、0755；rootless 将 0:0 落实为宿主用户及主组，避免 setgid 父目录影响映射。OverlayManager 把 lower 根目录属主和权限复制到 upper，排除宿主 umask 对合并根目录的影响；执行器新建 `/tmp` 后显式恢复 0:0、1777，已有目录不修改。

虚拟读取 passwd/group 等配置时，符号链接按组件解析：中间组件必须存在且为目录，不能通过普通文件或缺失路径的 `/../` 跳转读取另一个配置。文件目标的尾部斜杠同样按目录要求检查；合法目录内的相对链接和 `..` 保持可用。


## 文件指令

`COPY` 把本地普通文件、目录、符号链接与同一指令内的硬链接写入新层，保留 mode；新层 mtime 由显式固定 epoch 决定，默认新文件 UID/GID 为 0。`--chown` 可用数字或镜像内名称指定所有权，`--chmod` 当前只解析八进制。COPY/ADD 自动创建的目标和中间目录采用相同的传输选项，默认 0:0、0755；已有父目录保留原元数据。对 `ADD`，Python `tarfile` 根据内容识别 tar/gzip/bzip2/xz 并将成员安全映射到目标目录；普通 zip 作为文件复制。归档中的绝对路径、`..`、重复成员、`.wh.*`、穿过归档符号链接的成员会失败。归档硬链接目标必须是同一归档里的普通文件。

`layer.context_sources` 与 `check_context_source` 统一构建和静态检查的本地源路径边界。源通配符按目录组件展开，进入下一层之前先检查目录链接；递归 `**` 不调用会跟随链接的 `glob(..., recursive=True)`。普通符号链接可作为最终源节点保留；Windows junction 在 `lstat` 中表现为目录，需由兼容辅助函数识别并拒绝。目录树复制和缓存指纹在扫描子目录前复用同一检查，严格构建的锁定及快照复制也单独拒绝 junction。排除规则在目录树遍历时优先跳过不需要读取的节点。

文件指令对已有层的覆盖遵守新 layer 追加语义。目标目录路径包含符号链接、多个来源产生同名目标等复杂情况目前明确失败；符号式 `--chmod` 和 zstd tar 尚不支持。远程 HTTP(S) ADD 支持 checksum 和可选解包，未固定 checksum 的内容不会命中旧层缓存；具体范围见 Dockerfile 兼容表。

## 兼容性与错误处理

`EXPOSE`、`LABEL`、`VOLUME` 更新 image config；`VOLUME` 同时创建缺失的镜像内目录。构建临时工作区默认位于系统临时目录，若用户指定在 build context 内则拒绝，避免 `COPY .` 自我递归复制。Windows WSL 入口可转换 `--base-map` 内的 Windows 路径。基础镜像 config 类型、输出 manifest/config/layer 摘要、whiteout 形态和构建标签增加校验。

`.dockerignore` 的过滤语义尚未实现；有有效规则时构建会在读取基础镜像前失败，以免静默复制本应排除的文件。构建期变量由解析器保护字面量美元符，再由 Builder 按 ARG/ENV 作用域展开；不会把 shell 指令的运行期变量替换混入该流程。符号链接父路径下的层成员仍明确拒绝。支持范围以 [Dockerfile 兼容表](DOCKERFILE_COMPAT.md) 为准，不声称涵盖完整 Dockerfile 语义。

## 层缓存

`cache.py` 为 `COPY/ADD/WORKDIR/VOLUME/RUN` 保存指令级缓存。基础状态由 `FROM` 引用、基础 image config、base diff IDs 得出。每条指令将自身原文及结果 diff ID 加入连续状态键，因此前面的配置变化、文件内容变化或层结果变化会使后续缓存失效。`COPY/ADD` 额外遍历展开后的来源，哈希文件内容及路径、类型、权限、所有权、修改时间、链接关系；`RUN` 额外计入网络模式。源文件被同时修改时，构建目录应由调用方保持稳定。

条目放在 `entries/<key>.json`，实际未压缩层放在 `layers/<diffid>.tar`。当前指令缓存版本为 13，旧条目不能复用：版本 12 及更早的 RUN 物化可能通过下层符号链接误应用 whiteout 删除；版本 11 及更早可能根据无效账户文件链接或带 chown 哨兵值的层属主生成错误权限；版本 10 及更早的跨阶段 COPY 未读取硬链接 inode 的最终权限，COPY/ADD 还会遗漏源 PAX 扩展属性；版本 9 及更早的 RUN 可能使用宿主组、错误附加组或受 umask 影响的目录权限；版本 8 及更早的 COPY/ADD 可能遗漏新目录的属主与权限；版本 7 及更早的校验只识别首个 tar 头，可能信任负载截断的层。默认命中检查版本、键、大小与文件身份；身份变化或指定 --verify-cache 时重新核对 SHA-256、成员负载及结束位置。新层先核对结构，新副本关闭、刷新后在发布前重新校验摘要，再原子替换层与条目；失败不能把错误副本记为可信，既有条目保留。条目大小来自实际目标层。空层也能命中。`RUN` 命中后跳过 rootfs 物化、OverlayFS 与命令执行；未命中才进入 Phase 2 执行路径。基础镜像每次仍需读取与校验，最终归档每次仍要重新生成，因此缓存不保证固定倍数的加速。若外部网络资源改变，`RUN` 缓存无法自动感知，需 `--no-cache` 或修改相应构建输入。

显式 tar 的可信基础层缓存由 ImageArchiveReader 单独管理。跨文件系统发布时，临时副本必须核对预期 DiffID 才能移动到共享层路径；失败清理副本，保留 workspace 文件。基础层条目现在包含 version=2，旧无版本及 version=1 条目首次使用会重新提取、验证结构并写入新记录。旧条目可能对应首个 tar 头可读、实际负载已截断的层，不能继续信任。正常新版本命中的快速检查保留；无需手动清理缓存。CAS ref schema 仍为 1，读取层时另外执行结构检查。

## 多阶段状态

每条 `FROM` 开始新的 `ImageConfig`、layer 列表、`RootFSIndex`、工作目录、用户、shell 与缓存状态；前一阶段保存为不可再变的快照。`FROM <前序阶段>` 复制该快照的配置和层链，再继续添加新层。`FROM scratch` 从指定目标平台的空 rootfs 开始。`--target` 让构建在指定阶段结束，最终只输出该阶段的镜像。多个外部基础镜像可由 main.py 按 FROM 查库/自动拉取，也可用显式 `--base-map` 提供本地 tar；纯离线场景使用本地输入或 `--offline`。

`COPY --from` 从快照的**可见**文件树取源，因而先前的 whiteout 生效；普通文件内容从所属层流式读取，目录、权限、UID/GID 和符号链接按可表示的元数据写入目标层，mtime 归一化到固定 epoch。跨阶段硬链接会复制为普通文件，以免输出层引用未复制的硬链接目标。来源阶段状态键进入目标 `COPY` 的缓存键。

文件内容位置与 inode 元数据分开维护：创建硬链接后，链接头中的属主、mode、mtime 作用于所有仍可见的同 inode 名字；之后覆盖原路径会创建新 inode，旧别名保留原内容及更新后的权限。同一层重复出现也不能把新旧 inode 按内容成员重新合并。跨阶段 COPY 和 rootfs 导出/flatten 读取最终 inode 元数据。

COPY --from 与 ADD 解包保留 SCHILY.xattr 记录，但不复制源 PAX 的路径、大小、uid/gid 或时间覆盖项，避免撤销传输选项与固定 epoch。阶段来源路径从根目录解析，当前不支持指向外部镜像的 `COPY --from`。

## 双格式输出

`ImageArchiveWriter` 仍写 Docker save 布局。`OCIImageWriter` 用同一份 image config 和层链写 OCI Image Layout tar：

```text
oci-layout
index.json
blobs/sha256/<config digest>
blobs/sha256/<manifest digest>
blobs/sha256/<uncompressed layer digest> ...
```

单平台 index 含指定 `linux/amd64` 或 `linux/arm64` manifest descriptor 与完整标签 annotation；manifest 的 config、layer descriptor 分别使用 OCI config、未压缩 layer media type。层内容按摘要去重。发布前，验证器遍历所有成员，拒绝重复/额外成员，检查每个 descriptor 的 media type、size、SHA-256、平台、层顺序以及与 config `rootfs.diff_ids` 的对应关系。`--format both` 在两份归档都生成并验证后才发布；若发布第二份失败，会清理本次已经发布的第一份。Registry 推送及目标导入由独立客户端或目标环境完成。

OCI 校验、CAS 导入、Registry 单/多平台推送和多平台合并均通过 `image_reader.archive_members` 索引归档成员。索引允许 `./` 前缀，但拒绝规范化后重复或越界的成员；读取时使用索引中的 TarInfo，不能回到按未规范化名称查找的方式。CAS 导入保存原始 OCI manifest 字节，不因重新 JSON 序列化而改变其身份。Registry push 会将未压缩层转换成确定性的 gzip，因此远端 manifest 摘要可能变化，这是与原样导入 CAS 不同的处理步骤。

Docker 最终校验也使用该成员索引，要求所引用的 JSON 和 layer 为普通文件；manifest/config JSON 有 16 MiB 上限，拒绝非法 rootfs 和非 JSON 数值常量。两种 writer 校验器核对层摘要后，在同一可寻址流上检查 tar 头和成员负载边界，不二次完整读取负载；GNU sparse 使用实际存储的数据段长度。OCI 校验器返回四份元数据的原始字节快照，CAS 导入重开文件时与其比对，防止校验对象被一份同样合法的新归档替换。路径、whiteout 和执行语义仍由后续 rootfs 消费者验证。

`verify_layer_tar` 也用于 Registry 解压、Artifactory 转换、基础归档读取、CAS 解码/复用及指令缓存入库；不能等最终导出才发现坏层。检查包括负载和 padding 边界，以及迭代结束位置是否真为 EOF/零块，防止 `tarfile` 把后续坏文件头吞成结束。`read_image_json_file` 限量读取再解析 JSON，并返回同次读取的原字节；Artifactory 与 CAS 的 manifest/config/ref 共用这一入口。Registry config descriptor 在下载前受 16 MiB 限制，其他文件入口在解析前受该上限限制。拒绝 NaN/Infinity，所有转存仍保留原始字节。

Docker 归档入口共用 docker_manifest 校验 Config、Layers 与 RepoTags 的结构；RepoTags 只允许字符串数组、null 或缺省，不能把字符串子串或对象键当成标签。CLI 先确定唯一标签，再由 ImageArchiveReader 按配置平台选择实际条目。BaseImage 携带原始 config_raw/config_digest，以及来源提供时的 manifest_raw/manifest_digest；inspect 使用原始配置摘要报告 ImageID。业务路径明确选择 write_image 转存或 write_new 派生构建，前者强制检查原始字节、类型敏感的值一致性和摘要绑定，后者才编码新配置。仅 FROM 和无变化的前序阶段转接保留原始配置；ImageConfig 的内部缺省补全不再被误写为构建变更。详见 [镜像身份专项审计](IMAGE_IDENTITY.md)。

旧 tar 适配文件的配置字段、实际层与 CAS 完全一致，但 config 摘要因旧序列化方式不同而变化时，镜像库在同一引用锁内从 CAS 重建临时 tar，验证后原子替换。该恢复不更新 CAS ref，也不访问远端；失败保留原适配文件。内容不一致的 tar 仍拒绝复用。ImageID 是配置字节的 SHA-256，不能与 manifest digest 或整个交付 tar 的摘要互换，定义见 [OCI ImageID](https://github.com/opencontainers/image-spec/blob/main/config.md#imageid)。

构建、Registry/Artifactory 导出、严格封闭构建、优化器与多平台合并使用 `file_publish.py` 发布新交付文件。支持硬链接时，目标以原子创建方式出现，已有目标会使发布失败；跨磁盘先把完整文件复制到目标磁盘的独占临时文件，再创建链接。不支持硬链接的文件系统使用 `xb` 排他复制，仍禁止覆盖，但读者可能在复制完成前看到目标文件。调用方应等待命令成功再使用交付物；多产物发布没有全局事务锁。

## 可重复性约束

`reproducible.py` 校验并格式化构建 epoch，默认 0；build 命令可从环境变量 `SOURCE_DATE_EPOCH` 或显式参数取值；Fast 默认 0，需要其他值时显式传参。`ImageConfig.record` 不再读取墙上时钟。`LayerBuilder`、`OverlayManager` 对新层成员写固定 mtime；Docker/OCI writer 手工构造外层文件 tar 头，固定 mtime、UID/GID、mode，避免 `tarfile.add(path)` 把临时文件宿主元数据写进去。epoch 进入每个阶段的缓存状态键，并传给 `RUN` 环境。已有基础层不修改。当前测试验证相同输入经缓存命中与未命中、宿主文件 mtime 改变后，两种归档的 SHA-256 保持相同；还验证改变 epoch 会使缓存失效并改变产物。

`RUN` 命令可以自行产生非确定性字节，且程序不限制时间、随机数或网络调用。该机制只规范 builder 控制的序列化与时间戳，并为配合 `SOURCE_DATE_EPOCH` 的构建工具提供环境值；完全可重复的应用二进制仍取决于 Dockerfile、依赖和工具链行为。

## 交付证明

`sbom.py` 在最终可见 rootfs 索引上计算文件内容摘要，复用原始 layer tar 而不解包到宿主机。它输出固定序列化的 SPDX 2.3 JSON，并检测 dpkg/APK/Python/Maven 元数据。`attest.py` 对 Docker/OCI 归档、SBOM、Dockerfile、context 清单、基础归档计算 SHA-256，构造 SLSA provenance v1 的 in-toto Statement。证明中用固定逻辑 subject 名称，避免输出路径影响可重复性。`ed25519.py` 用纯 Python 实现 RFC 8032 签名；DSSE 封套按 PAE 规则签署 Statement 原始字节。签名和校验只依赖标准库。

构建事务先在工作区生成并验证镜像，随后生成证明和可选签名，最后发布所有产物。独立验收命令要求明确提供受信任公钥和全部归档，只确认签名及交付文件摘要关系，不替代构建环境可信性审计。`RUN` 的动态网络来源不自动纳入 `resolvedDependencies`；SBOM 是有明确检测范围的清单，不是漏洞或许可证审查结果。

标准参考：[SPDX 2.3](https://spdx.github.io/spdx-spec/v2.3/)、[SLSA provenance v1](https://slsa.dev/spec/v1.0/provenance)、[DSSE envelope](https://github.com/secure-systems-lab/dsse/blob/master/envelope.proto)、[RFC 8032](https://www.rfc-editor.org/rfc/rfc8032)。

随包的纯 Python Ed25519 实现没有经过独立安全审计，Python 大整数运算并非常数时间。生产签名应结合组织的安全评估和密钥保护要求；私钥不得提交到仓库或写入镜像。

## RUN 隔离

`hardened` 是默认模式：在原有 mount/PID/network namespace 和 chroot 的基础上，先完成 proc/dev 挂载，再设置 `no_new_privs`、清空 capability bounding set 及当前 capability 集，最后装入 seccomp BPF 过滤器并 `exec`。过滤器先检查审计架构（x86_64/aarch64），x86_64 另拒绝 x32 syscall 编号，随后以 EPERM 拒绝挂载、namespace 切换、ptrace、BPF、内核模块等系统调用；其他调用允许，因此不是完整白名单。架构或内核不支持时失败，不回退到弱模式。

`rootless` 在每条 `RUN` 的子进程中先创建 user namespace，写入单条 UID/GID 映射，禁止 `setgroups`，随后创建其他 namespace。父构建进程始终保留非 root 宿主身份。`RootFSMaterializer` 仅接受 0:0 的 image layer 元数据，将其物化为当前宿主用户拥有的文件；OverlayFS 用 `userxattr`，转 OCI layer 时将宿主 UID/GID 还原为 0:0。非零 UID/GID、设备节点、无法表示的 xattr、非 root `USER` 均明确失败。`/dev` 中少量标准设备采用只在子挂载命名空间生效的 bind mount；不需要宿主 `mknod` 权限。

这两种模式都不能封锁所有系统调用或限制 CPU、内存、磁盘资源。Linux user namespace/OverlayFS 的可用性由目标内核、文件系统和发行版策略决定；当前 Windows 测试不能证明目标 Linux 可运行。参考：[user_namespaces(7)](https://man7.org/linux/man-pages/man7/user_namespaces.7.html)、[OverlayFS userxattr](https://docs.kernel.org/filesystems/overlayfs.html)、[seccomp(2)](https://man7.org/linux/man-pages/man2/seccomp.2.html)。

## Registry 客户端

`registry.py` 通过标准库 `http.client` 实现 OCI Distribution / Docker Registry v2 的 manifest、blob、POST+PUT 上传接口。TLS 使用系统信任链或指定的私有 CA；默认拒绝 HTTP。认证支持同源 Basic 和 Bearer challenge，token realm 默认限定 Registry 主机，跨主机 token 服务需显式授权。GET blob 重定向到不同 HTTPS 主机时不会转发 Authorization。上传 Location 必须仍指向原 Registry 的同一 repository，避免把 blob 和凭据发到任意地址。

拉取优先请求 OCI/Docker schema 2 image manifest 或 index；index 选指定的 `linux/amd64` 或 `linux/arm64` 平台。每个 manifest 和 blob 做 digest/size 校验，gzip 层解压后再核对 config 的 `rootfs.diff_ids`，随后用原有 writer 输出 Docker tar、OCI tar 或两者。推送前先验证本地 OCI tar；Docker save 输入会先转换为 OCI 布局。未压缩层确定性 gzip 压缩，按 digest 查询远端已有 blob，缺失的才上传；最后 PUT 新 manifest 并校验 Registry 回传 digest。构建缓存与 Registry blob 存储分开，构建器仍可纯离线执行。

本机回环 HTTP 模拟测试覆盖 Bearer token、Docker/OCI 归档推送、gzip 层、多架构索引选择、按 digest 拉取、blob 篡改拒绝和凭据失败。尚未连接真实 Harbor，也未验证其代理、证书、项目策略或断点续传行为。标准参考：[OCI Distribution](https://github.com/opencontainers/distribution-spec/blob/main/spec.md)、[OCI image media types](https://github.com/opencontainers/image-spec/blob/main/media-types.md)。

## 平台传播与双平台归档

`platforms.py` 将目标限制为 `linux/amd64` 或 `linux/arm64`。命令行平台传给每个阶段；字面量 `FROM --platform=...` 覆盖本阶段；`FROM scratch` 的 config 采用该平台的 architecture。`ImageArchiveReader` 在 Docker save tar 中按 config 平台选择同名的基础镜像，并拒绝平台不匹配。`--base-map` 可按引用与平台两级映射。阶段间 `COPY --from` 复制可见文件内容，保留最终阶段的目标 architecture，不能翻译源阶段中的机器码。架构与模拟执行模式进入 `RUN` 缓存键。

`multiarch.py` 校验两份单平台 OCI tar 后，按 amd64、arm64 顺序构造 index，去重 blob 并校验 descriptor digest、size、config architecture 和 layer diff ID。外层 tar 与 index 序列化由固定 epoch 控制。`registry.py push-index` 将两个平台的未压缩层确定性 gzip 压缩，先按 digest 上传子 manifest 和 blob，再发布 index tag；`pull --platform` 从该 tag 选择对应子镜像。此 OCI index tar 不作为单文件 Docker save tar；需要 `docker load` 时单独构建或拉取各平台的 Docker tar。

本机与目标架构不同时，若该阶段有 `RUN`，默认拒绝执行。显式 `--allow-emulated-run` 仅在 Linux 上检查启用的目标 QEMU binfmt_misc 处理器及 F 标志，然后走原有执行器；不会安装 QEMU、注册 binfmt 或模拟 CPU。实际成功还取决于目标 rootfs、内核、namespace、OverlayFS 与沙箱权限。无 `RUN` 时无需 QEMU，也无需 Linux。当前测试用合成基础镜像、模拟 Registry 和拒绝路径验证双平台行为；尚未实测真实外架构 `RUN`。

## ARG 与临时 RUN 挂载

解析器将 `ARG`、`RUN --mount=type=cache/secret` 作为显式结构，不解释不支持的 mount 类型。全局 ARG 可供 `FROM` 使用，阶段 ARG 作为当前阶段的构建环境传给 `RUN`，CLI 值进入构建状态键，缓存不能错误复用不同 ARG 值的结果。ARG 不持久化为镜像 `Env`。变量展开支持默认值、替代值及部分模式处理；准确语义与词法边界见 [Dockerfile 兼容表](DOCKERFILE_COMPAT.md)。

cache mount 的宿主目录以目标平台和 mount id 的 SHA-256 命名，位于指定 cache 根目录；Linux `flock` 将同一目录的写入串行化。secret mount 从 CLI 指定的文件只读 bind 到命名空间内指定路径。两者都在 OverlayFS merged rootfs 之后、chroot 之前安装；对应 mount 目标从 upperdir 转 layer 时排除。mount 本身不会写入 image config；secret `RUN` 不查找或写入指令层缓存。父目录可能作为空目录出现在新层，符合路径存在的效果。若 RUN 主动将 secret 内容写到非挂载路径，仍会进入产物；本工具不提供数据防泄漏沙箱。

## 离线分析与保守删层

`optimizer.py` 复用 Docker save 读取器和 OCI writer 的校验路径，把每个未压缩层暂存到独立工作区，再用 `RootFSIndex` 计算最终可见路径。逐层报告 tar 字节、普通文件有效负载、在最终 rootfs 中已被覆盖或删除的文件字节；相同 SHA-256 的文件内容分组用于定位重复拷贝。隐藏字节及重复字节可能交叉，不能相加当成节省空间估计。

自动优化逐层构造“去掉该层”的候选链，比较候选与原链的最终路径、类型、内容摘要和 tar 元数据签名。只移除签名一致的完整层，删除其对应的非空 history 条目并重写 diff IDs。含硬链接或特殊文件的镜像直接拒绝自动删层；这样避免对 inode 关系和设备语义作不可靠推断。新归档经过 writer 验证，并重新读取比较最终 rootfs 签名。这个检查只保证本项目模型下的最终 rootfs 一致，不替代 Docker/Podman 的真实运行验证；镜像 digest 与既有旁路证明会改变。

缓存审计只扫描 `entries/*.json` 和 `layers/*.tar`。它验证当前缓存版本、key、diff ID、层大小、SHA-256 和 tar 格式；失效条目及不再被有效条目引用的层可在显式 `--prune` 时删除。`run-mounts` 与构建中的临时文件不在清理范围。清理与构建不能并发进行。

## 独立语义验收

`conformance.py` 不调用 `ImageArchiveReader`、`RootFSIndex` 或两个 writer 的验证函数来判断样例输出，而是独立解析 Docker save/OCI tar、核对每层 diff ID，并实现 OCI whiteout/opaque 规则，得到最终文件树摘要。固定样例逐项断言内容、uid/gid、mode、ENV/CMD/ENTRYPOINT、VOLUME/SHELL、阶段隔离、链接及缓存失效；运行配置对照还包含 HEALTHCHECK、STOPSIGNAL、ONBUILD，不能将这些字段的差异忽略为一致；Docker 与 OCI 两种输出还会互相比对。这样可发现构建器与其自带验证器共同遗漏的语义错误。

可选 `reference` 模式在另一台有 Docker daemon 的机器上对同样 Dockerfile 执行 Docker 构建，将参考镜像与本工具镜像的最终配置和 rootfs 作差异比较，再通过 Docker load/inspect/create/export 检查 daemon 是否能解释本工具产物。`compare` 模式将用户已有归档与 Docker 对其 Dockerfile 的构建结果比较。两者都会明确标记 Docker 对照已执行；离线通过不等于 Docker 兼容通过。比较时忽略 tar mtime 和部分隐式目录表示，可能仍有不同 Docker/BuildKit 版本引起的差异；报告保留字段级和路径级证据，交由目标环境确认。

`linux` 模式封装现有真实 RUN 冒烟测试与 Phase 13 cache/secret mount 测试，必须在 Linux/WSL 和满足执行器权限条件的基础镜像上运行。所有模式都输出明确的执行范围及 JSON 结果；未安装 Docker 不会被报告为 Docker 对照通过。

## Docker save 归档

输出 tar 顶层包含：

```text
manifest.json
<config-sha256>.json
repositories
<chain-id-1>/layer.tar
<chain-id-1>/VERSION
<chain-id-1>/json
<chain-id-2>/layer.tar
...
```

`manifest.json` 是数组，单镜像项含 `Config`、`RepoTags`、按底层到顶层排列的 `Layers`。镜像 config 的 `rootfs.diff_ids` 也是同序的**未压缩 layer.tar** SHA-256；config 文件名是其 JSON 字节的 SHA-256。`history` 中产生文件系统层的条目对应 diff ID，纯元数据指令标记 `empty_layer`。`repositories` 与各层的 `VERSION/json` 是兼容性元数据。Docker save 归档的具体布局以 Docker 的加载实现为准；OCI image config 和层格式分别规定 diff ID、history、文件属性及 whiteout 语义。

## RUN 执行模型

1. 在 Linux 上，从已校验的 base layers 和 Phase 1 新层安全还原真实 rootfs，依次应用 OCI whiteout，保留 uid/gid、mode、mtime、链接和可表示的 xattr。拒绝越界路径。Windows 原生模式不物化 Linux rootfs。
2. 为每条 `RUN` fork 子进程，在子进程中 unshare mount/PID namespace，默认也 unshare network namespace；把 mount 传播设为 private 后，挂载只读 lower rootfs、同文件系统上的 `upperdir/workdir` 与 `merged`。OverlayFS 不可用时失败。父构建进程不进入这些 namespace。
3. 在命名空间内挂载最小 `/proc`、tmpfs `/dev`、`/dev/pts`，chroot 到 merged，然后按 Dockerfile 当前 `ENV`、`WORKDIR`、`USER`、`SHELL` 执行命令。PID namespace 在 unshare 后 fork；等待并传播退出码，非零即失败。子进程退出时命名空间内的挂载消失。`/sys`、DNS 注入和 cgroup 资源限制尚未实现；seccomp 收紧由前述 hardened/rootless 沙箱负责。
4. 把 upperdir 的内容**转换**为 OCI/Docker layer：OverlayFS 的 0/0 字符设备或 `overlay.whiteout` xattr → 空 `.wh.<name>` 文件；`overlay.opaque=y` → `.wh..wh..opq`。保留基本元数据、符号链接和硬链接；遇到无法可靠表示的 metacopy、redirect 或非 UTF-8 xattr 会失败，不能直接 tar upperdir。完成后更新可见 rootfs 和 config；下一个 `RUN` 在上一个结果上执行。
5. `USER` 和 `SHELL` 已先于执行器加入状态模型。`hardened` 模式需要 Linux root；`rootless` 模式需满足前述单 UID/GID 和内核约束。仍需 Linux/WSL 真机集成验证及更多 OverlayFS 组合测试。

Windows 上的可选 WSL 入口只负责**切换执行地点**：使用 `wsl.exe` 执行本项目 `main.py`，把 Windows 驱动器路径映射到 WSL，工作区放在 Linux `/tmp`。WSL 发行版、Python 3、文件可见性、架构与必要内核权限仍需要在目标机验证。没有可用 WSL 时，含 `RUN` 的构建明确失败；无 `RUN` 的构建不需要 WSL。

`chroot` 单独使用只改变路径解析，不是安全隔离，不能以“直接 chroot 后执行”冒充完整的容器环境。WSL 桥接缺少显式沙箱参数时，也使用 `hardened` 默认值，不退回早期的 `legacy` 模式。

## 工程目录

```text
pyimagebuilder/
  main.py                 CLI
  builder.py              指令编排与构建事务
  dockerfile_parser.py    Dockerfile 词法与指令语法
  image_reader.py         本地 Docker save 读取、层校验
  rootfs.py               可见文件树与 whiteout 应用
  rootfs_archive.py       rootfs export/import/flatten 独立入口
  image_changes.py        import/flatten 的运行配置变更
  layer.py                新 layer.tar 构造
  cache.py                持久化指令层缓存
  image_config.py         配置和 history 更新
  image_writer.py         Docker save 归档生成、内部验证
  oci_writer.py           OCI Image Layout tar 生成、内部验证
  reproducible.py         固定时间戳与确定性归档头
  compat.py               Python 3.7 与较新字符串/pathlib API 的差异适配
  sbom.py                 SPDX 2.3 文件和组件清单
  attest.py               SLSA provenance、DSSE 封套与离线校验 CLI
  ed25519.py              纯 Python Ed25519 签名及验证
  overlay.py              OverlayFS 挂载与 upperdir 层转换
  executor.py             namespace/chroot/RUN 执行
  sandbox.py              post-setup seccomp BPF 过滤器
  registry.py             Registry v2 pull/push CLI 与 TLS/Bearer 客户端
  platforms.py            amd64/arm64 目标平台与外架构 RUN 前置检查
  multiarch.py            双平台 OCI index tar 合并与验证 CLI
  optimizer.py            单平台镜像分析、保守删层与缓存审计 CLI
  conformance.py          独立离线语义样例与 Docker 参考对照 CLI
  hermetic.py             本地输入锁、快照与严格无 RUN 构建
  fast.py                 WAR/dist.zip 的配置式单文件构建入口
  artifactory_download.py Artifactory 制品目录下载与 Docker tar 转换
  image_store.py          main.py pull 与 build 的本地基础镜像缓存
  cas_store.py            SHA-256 blob、引用索引、OCI/Docker 导入与 Docker tar 导出
  rootfs_materializer.py  RUN 前真实 rootfs 还原
  errors.py               明确的错误类型
```

## 已实现范围与明示限制

具体 Dockerfile 指令和 flag 范围以 [DOCKERFILE_COMPAT.md](DOCKERFILE_COMPAT.md) 为准。无 `RUN` 时的“还原 rootfs”是逻辑索引；有 `RUN` 时才物化真实 rootfs。当前不应把 mount namespace/chroot 误当成完整安全沙箱。

本机可验证归档布局、diff ID、config SHA-256、模拟根文件树及执行器编排；当前没有可用 Linux root/WSL 发行版，因此真实挂载与 `RUN` 尚未实测。另一台装有 Docker Engine 的机器执行 `docker load` 与应用启动后，才能确认与目标 daemon、基础镜像和应用的实际兼容性。

严格封闭模式先校验完整 context、Dockerfile、基础 tar 和构建参数的输入锁，再复制所有文件到独立临时工作区。快照自身再次校验后，禁用 RUN 与持久缓存，并用显式时间戳构建；发布前复核原输入，输出归档 SHA-256 写入伴随报告。对不变的输入、源码、Python/平台指纹，这给出可重现的本地文件输入闭包。普通 RUN 模式仍可使用，但严格模式拒绝任意 RUN，因为命令能观察和读取超出文件快照的宿主状态。此设计不构成针对恶意宿主进程的 OS 安全沙箱。

参考：[OCI image layout](https://github.com/opencontainers/image-spec/blob/main/image-layout.md)、[OCI manifest](https://github.com/opencontainers/image-spec/blob/main/manifest.md)、[OCI image config](https://github.com/opencontainers/image-spec/blob/main/config.md)、[OCI layer](https://github.com/opencontainers/image-spec/blob/main/layer.md)、[Linux OverlayFS](https://docs.kernel.org/filesystems/overlayfs.html)、[chroot(2)](https://man7.org/linux/man-pages/man2/chroot.2.html)。
