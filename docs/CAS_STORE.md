# 本地 CAS 镜像库

[文档目录](README.md) · [项目首页](../README.md)

本页解释基础镜像的落盘和复用。日常设置见 [配置指南](guides/CONFIGURATION.md)，查命令见 [命令速查](guides/COMMANDS.md)。

| 常见问题 | 当前行为 |
| --- | --- |
| 镜像放在哪里 | `cache.imageStore` 或 `--image-store` 下；CAS 位于其中的 `cas/` |
| 构建是否先生成 base.tar | 普通 build/Fast 可直接消费 CAS；显式本地 tar 路径仍保留 |
| 远端 layer 是否无需处理 | 压缩 blob 仍需解码和 DiffID 校验，已验证未压缩层可以复用 |
| 为什么磁盘比下载量大 | 可同时保留压缩 blob、未压缩层和导出 tar |

```text
image-store/
└── cas/
    ├── blobs/sha256/    原始 config、manifest、传输层与已解码层
    ├── refs/            引用 + 平台 + 来源 → blob 的 JSON 索引（schema 1）
    └── locks/           稳定的引用锁文件
```

## 镜像身份

原始 config/manifest 的身份契约、转存与派生的区别、全项目审查结果见 [镜像身份与原始 JSON](IMAGE_IDENTITY.md)。CAS 发布前会核对 manifest/config/ref 绑定；导出不会因字典重新编码改变 ImageID。仅 FROM 的构建也保留原配置字节，真正追加指令才产生派生配置。

## 存储位置与内容

`cache.imageStore` 下的 `cas/` 保存按 SHA-256 命名的 config、manifest 和 layer blob；`cas/refs/` 用版本为 1 的 JSON 文件把镜像引用、平台和来源映射到这些 blob。引用文件最后以原子替换发布。重复 blob 只保存一份；读取和导出时重新核对摘要。重复导入仍完整核对本次输入的大小和 SHA-256，不能用旧 blob 的正确内容代替新输入校验；不匹配或读取失败时不发布新 ref。正确输入复用旧文件，不重复写盘。`cas prune --dry-run` 可查看无引用 blob，确认后运行 `cas prune` 清理。被现存镜像引用的压缩层及其已校验的未压缩 diff ID 层会保留。清理期间不要同时导入、拉取或构建。

## 并发与引用锁

`cas/locks/` 保存按镜像引用、平台及来源区分的锁文件。`pull`、`load`、`rmi`、按需导出受管理 tar，以及 CAS 引用发布共用线程与跨进程互斥，防止失败恢复回退其他成功操作。竞争操作立即报 `Image reference is busy`，请等当前操作完成再重试；不同引用或来源仍可独立更新，拉取内部的 layer 并发下载保持可用。同线程嵌套调用可重入。

锁文件不会在解锁时删除；进程退出由操作系统释放锁，看到 `.lock` 文件本身不表示存在残留锁。运行期间不能删除或替换这些文件。同一镜像库的并发进程应使用本次更新后的程序；旧版本不会遵守新锁。锁依赖文件系统提供的内核文件锁，网络共享盘需另做目标环境验收。这是单引用操作保护，不是全局清理锁或多个镜像的统一事务，`prune` 仍不能与导入、拉取或构建并行。

## 导入、导出与来源

`pull`、`load` 和 Fast 的基础镜像共用这一仓库。Registry 与 Artifactory 拉取保留压缩 layer 的原始 digest；`load` 导入 Docker archive 的未压缩 layer。`cas import ... --format oci` 可以直接导入本工具生成的单平台、未压缩 OCI layout tar。`cas inspect IMAGE --source registry|artifactory|local` 校验引用及 blob；`cas export IMAGE -o image.tar` 生成单平台 Docker save 归档。相同标签在不同来源中可共存，查看或导出时需指定 `--source`。

### OCI 导入的当前边界

OCI 导入按已验证的原始 manifest 字节入库，保留其 digest、格式空白和 manifest 内注解，不重新生成 manifest。归档成员可带 `./` 前缀；导入、推送、多平台合并及校验共用规范化路径索引，规范化后同名的两个成员会报重复错误。当前仍要求单平台导入的层为未压缩 tar，且布局符合本工具校验器支持的严格成员集合；这不代表已接受任意第三方 OCI 布局。

校验器返回 `oci-layout`、index、manifest 和 config 的原始 JSON 字节快照，每份最多 16 MiB。导入重开文件时，先与快照逐字节比对并核对成员集合、类型和层大小，再按快照中的摘要复制层。即使输入被另一份摘要自洽的合法归档替换，也会拒绝发布引用；使用 `--replace` 时失败保留已有引用。校验同时检查层可作为 tar 读取及成员负载是否截断，摘要一致不能代替 tar 结构检查。

CAS 的 manifest/config/ref 文件同样限量读取，每份最多 16 MiB，并拒绝 NaN/Infinity。Registry 拉取会在下载 config 前检查 descriptor 大小；Artifactory 文件在下载后、解析前检查上限。正常 config 的原始字节不会重新序列化，ImageID 保持不变。

此前导入过的 OCI 引用仍可使用；若旧实现曾重建 manifest，重新导入带不同原始摘要的同标签归档时，需要 `--replace` 才更新镜像身份。基础 layer blob 可继续去重复用，不要求清空镜像库。

## 构建如何复用层

普通 `build` 和 Fast 现在直接从 CAS 读取基础 config 与 layer；首次从 Registry/Artifactory 下载供构建使用时，也可直接校验入库，跳过 Docker tar 的转换与打包。两种下载器都在发布引用前核对本次传输 blob、解压后 DiffID 和 tar 结构，并保存原始 blob 与已验证的未压缩 tar，随后构建直接复用。Artifactory 的 CAS-only 路径逐层解码、入库并删除临时解码文件，不组装中间 Docker save tar。两种层表示的磁盘占用会叠加，实际存储量由压缩率决定；保留它们是为了兼顾原始传输摘要和构建速度。显式 `--base-tar`、`--base-map` 的旧式路径仍由 Docker archive reader 读取。独立 `pull`、`save` 和 `cas export` 仍会生成 Docker tar，供 `docker load` 使用；因此仓库中同时存在 CAS 和归档时，磁盘用量还会增加。当前层解码路径支持 gzip 和未压缩 tar，不支持 zstd；遇到 zstd 会明确报错，不能假定安装第三方模块后就会自动接入。

仅识别开头是否像 tar 不足以证明完整性。下载和读取路径共用层结构检查，核对成员负载、padding 及结束位置；CAS 新解码层在入库前检查，已有解码层在使用时再次检查。当前指令缓存版本为 22，路径规则见 [路径处理规则](PATH_POLICY.md)，权限规则见 [镜像权限规则](PERMISSION_POLICY.md)；缓存覆盖根目录元数据、whiteout、硬链接、RUN 层校验和指令应用后的发布规则。显式 tar 的基础层缓存版本为 3，旧条目自动重新构建或验证。CAS schema 仍为 1，无需清库或重新下载正常镜像。`import_registry` 的低层接口仍允许保存压缩传输 blob，不独立保证 DiffID 和解压成功；标准 Registry 与 Artifactory 下载路径负责在发布前完成这两项验证。

已有某个 DiffID 的解码缓存，只能证明该缓存文件的内容，不能证明新下载的压缩 blob 对应同一个 DiffID。远端刷新时仍解码并验证本次下载内容，不以旧缓存命中跳过这一绑定校验。Artifactory 下载接口在引用锁内保存旧 ref，完成验证后发布新 ref；后续打包失败则恢复旧 ref。失败前已经保存的独立 blob 可由 GC 回收，不算成功下载的镜像。

直接调用 `registry.pull(..., cas_store=...)` 同样会在引用锁内保存旧 ref，并提交 CAS 与输出归档。发布失败恢复旧 ref；首次拉取失败不留下新标签。双格式输出的第二个目标冲突时，只清理本次成功发布的第一个文件，保留竞争写入者的文件。归档清理失败仍尝试恢复引用，并将清理错误报给调用方；此时可能需要处理未能删除的输出文件。发布阶段的中断也执行恢复。锁保护同一引用的竞争更新，双输出仍是逐个发布，请等待调用成功后再使用。

CAS ref 的层描述符现在可带布尔字段 `diff_id_verified`，记录工具已验证该传输 digest 与 DiffID 的对应关系。Registry/Artifactory 下载器仅在本次解码校验成功后记录 `true`；不会从远端 manifest 的同名字段接收这个状态。低层 `import_registry` 默认不记录已验证状态。

旧 ref 或低层导入没有这项记录时，即使同 DiffID 的 tar 已存在，`open_base` 也会解码当前压缩 blob，核对实际输入的摘要及 tar 结构。所有层通过后，在引用锁内补写校验状态；已删除或被替换的引用不会被恢复或覆盖。后续构建复用已验证解码层，仍检查 blob 摘要与 tar 结构。保存新解码结果时，已有同摘要文件也不能代替对新输入的校验。

这个可选字段属于可信本地缓存元数据，不是签名或远端证明。CAS schema 仍为 1，原始 manifest/config、ImageID 和层摘要均不变；正常旧镜像首次使用会补验，无需清库或联网重新下载。错误的 blob→DiffID 对应关系会报错并保留原引用供检查，不自动替换为另一份镜像。

### 交付副本校验

`save` 和 `pull -o` 校验的是实际生成的交付文件，包含选定平台、层 DiffID、tar 结构与配置原始摘要。
旧镜像库只有 tar、没有 CAS ref 时同样执行成品层校验，不把存在的 tar 自动当作有效镜像。
配置字段相同但原始字节变化，也会因 ImageID 改变而拒绝。

复制或校验失败时清理本次创建的输出，缓存源文件保留；已有同名输出不会覆盖或删除。
校验采用流式读取，不额外解包层，但会增加一次成品层读取。
`pull -o` 最后报告交付文件的摘要和大小；复制后的缓存变化不会使报告变成另一份文件的摘要。

## 发布事务与失败重试

独立下载入口也把工作目录退出放在引用事务内；清理抛异常时恢复旧 ref，
并撤销本次创建的归档。CAS Docker 导入在临时目录成功退出后才发布 ref，
CAS 导出则在工作目录清理失败时撤销本次输出，避免下一次重试被同名文件阻塞。

ref 的原始字节快照不重新编码，--replace 可修复损坏的旧 ref；
失败恢复保留原字节，正常 resolve 仍严格校验。更新和恢复共用引用锁，
锁覆盖独立下载入口的下载至退出过程。规则与边界见 [缓存说明](CACHE_POLICY.md)。

### 刷新与已有 tar 校验

复用已有 tar 适配文件时，如果已有 CAS 引用，会核对两者的 config，并直接流式检查 tar 内各层的 DiffID；不一致或内容损坏时明确失败。默认构建直接使用 CAS，不触发这次 tar 检查。普通构建复用已存在的镜像；`pull` 或 `build --pull` 刷新远端标签。目前刷新仍会下载各层，CAS 去重减少的是最终存储重复，尚未跳过远端已存在的层下载。

### 保留原始配置与旧 tar 恢复

CAS 导出 Docker tar 时保留原始 config JSON 字节，ImageID 不因重新排序字段或删除空白而改变。复用旧 tar 时，如果配置字段相同、实际层 DiffID 也全部通过校验，但配置原始字节摘要与 CAS 不同，会在引用锁内离线重建适配 tar：先验证新 tar，再原子替换；失败保留旧 tar/ref，不联网下载。配置字段不同或层内容损坏仍报错，不按旧格式兼容处理。原始 manifest digest 与 config 的 ImageID 是不同概念，格式转换或压缩方式变化仍可能改变 manifest digest。

### 多平台归档与选定引用

多平台 Docker tar 导入时，可通过 `load --tag IMAGE --platform linux/arm64` 注册所选变体；只有一个唯一标签时 --tag 可省略。原 tar 中的其他标签/平台不会因此自动注册为 CAS ref，也不会从适配 tar 中裁掉；显式 cas export 则按选定 CAS ref 生成单镜像 tar。

## 常用命令

示例：

```powershell
python .\main.py pull harbor.internal/project/base:1 --image-store D:\image-store
python .\main.py cas inspect harbor.internal/project/base:1 --source registry --image-store D:\image-store
python .\main.py cas import D:\images\base.oci.tar base:1 --format oci --image-store D:\image-store
python .\main.py cas export base:1 --source local -o D:\images\base.tar --image-store D:\image-store
python .\main.py cas prune --dry-run --image-store D:\image-store
```

## 冲突、删除与清理

镜像引用冲突时 `cas import` 默认拒绝覆盖，显式 `--replace` 才更新。普通 `pull` 是刷新远端标签。`rmi` 在引用锁内删除对应来源的 tar 适配文件和 CAS 引用，归档删除失败时恢复引用；引用忙时不会先删除 tar。`rmi` 不再自动执行全库 GC，以免删除其他正在导入但尚未发布引用的 blob。因此删除后 CAS 占用可能暂时保留，其他镜像共享的层也继续保留。

引用删除调用自身失败或中断时也恢复原始 ref；直接 CAS ref 删除遵循同一规则。如果兼容 tar 已删除才抛异常，会恢复 CAS 引用供继续使用与重试，不重建 tar。
`image prune` 在年龄筛选前拒绝伪装成 .tar 的目录；CAS 预览排除的 ref 仍需通过普通文件和非链接检查。这些预检完成后才开始删除，仍要求停止镜像库上的并发写入。

停止该镜像库上的构建、导入、拉取和其他存储操作后，使用 `main.py cas prune --dry-run` 预览，再用 `main.py cas prune` 回收孤立 blob；按同一配置或 `--image-store` 指向原镜像库。`cas prune` 不删除标签或 tar。清理前核对保留 ref 的身份、原始 manifest/config 摘要、层描述符和 DiffID 对应关系；损坏引用会使清理在删除 blob 前失败。GC 不重新读取全部大层计算摘要，完整层内容校验仍在镜像读取/导出时执行。`image prune` 的删除前预览也使用这一可达性校验。

## 固定 digest 引用

按 digest 固定的 Registry 引用在 CAS 中保持 `repo@sha256:...` 原样。需要 Docker save tar 时，归档内使用稳定的 `repo:pulled-<摘要前 12 位>` 标签；这只是导出标签，CAS 的检索键仍是原始 digest 引用。`image prune --dry-run` 的 `reclaimable_bytes` 包含候选 tar 与删除其引用后可回收的 CAS blob。
