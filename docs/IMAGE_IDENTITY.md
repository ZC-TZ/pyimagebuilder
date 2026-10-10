# 镜像身份与原始 JSON：维护契约

[文档目录](README.md) · [项目首页](../README.md)

本页定义镜像读取、Registry/Artifactory 下载、CAS、tar 适配缓存、构建、多阶段快照、优化、推送、平台合并与签名之间的身份保留规则。转存时必须同时保留原始 JSON 字节、摘要和类型敏感的配置值；仅给个别调用补 `config_raw`，不足以保证整条链路的镜像身份稳定。

## 1. 先区分四种摘要

| 摘要 | 计算对象 | 会因什么改变 |
| --- | --- | --- |
| ImageID / config digest | config JSON 的原始字节 | 字段变化，或仅空白、键顺序、转义和数值表示变化 |
| manifest digest | manifest JSON 的原始字节 | 配置描述符、层描述符、注解变化，或仅重新编码 |
| index digest | image index JSON 的原始字节 | 平台条目、引用注解、扩展字段变化，或仅重新编码 |
| tar SHA-256 | 整个交付归档 | 包装、成员顺序、tar header、标签等变化；即使 ImageID 相同也可能不同 |

层的 transport digest 针对实际传输 blob，DiffID 针对未压缩 layer tar。gzip 重压缩可能改变 transport digest 和 manifest digest，而保持 DiffID 与 ImageID。

规范依据：[OCI config / ImageID](https://github.com/opencontainers/image-spec/blob/main/config.md#imageid)、[descriptor / digests](https://github.com/opencontainers/image-spec/blob/main/descriptor.md#digests)、[image index](https://github.com/opencontainers/image-spec/blob/main/image-index.md)。解析后的对象用来检查结构和执行指令，不能代替原始字节计算或恢复已有身份。

## 2. 为什么以前会反复出现

1. **读取后只传 dict。** 原始字节在组件之间丢失，写入器只能重新编码。
2. **同一写入接口兼做转存和新构建。** `config_raw=None` 默认合法，漏传不会报错。
3. **假定 build/optimize 一定产生新镜像。** 仅 `FROM`、未裁掉任何层时，实际没有配置变化。
4. **把初始化补全当成构建变更。** `config:null` 补成 `{}`、缺省 history 补上导入记录，属于构建工作状态；没有执行实际指令时不应写回基础配置。
5. **只测规范化 JSON。** 测试输入本来就与 writer 的编码相同，漏传原始字节也不会暴露。
6. **只检查 config。** manifest/index 重新编码或丢失注解，仍会改变另一种身份。
7. **用 Python 的 `==` 判断对象未变。** `True == 1`、`1 == 1.0`、`-0.0 == 0.0` 会隐藏配置变更。

## 3. 现在的接口契约

### 原始配置和 manifest 随镜像对象传递

`BaseImage` 同时携带解析配置、原始 `config_raw`、`config_digest`、有序层路径，以及来源提供时的原始 `manifest_raw` 和摘要。`original_config_bytes()` 核对原始字节、解析对象和声明摘要；`original_manifest_bytes()` 还核对 manifest 与 config 的绑定。

原始字节使用不可变 `bytes`。修改解析字典不能改变原始字节；尝试用已修改字典转存会失败。类型敏感的 `same_json_value` 仅用于检查变更，**不用于生成任何已有镜像的 digest**。

### 两种明确的写入操作

```python
# 转存：缺少原始配置、配置已修改或摘要不一致，创建输出前报错。
writer.write_image(output, image, tag)

# 派生构建：确实修改了 config/history/layers，稳定编码新配置。
writer.write_new(output, config, layers, tag)
```

writer 的公开写入入口只有 `write_image` / `write_new`，用途不明确的旧 `write(...)` 已移除。`_write` 是内部实现，业务代码和测试夹具均应选择明确入口。转存需要构造或保留携带原始字节的 `BaseImage`，不要从只读分析返回的 dict 重建已有镜像。

OCI `write_image` 会创建新的 layout index；它保证 config 身份，并在有原始 manifest 且最终描述符不变时复用 manifest 原字节。它不承诺整个 OCI 归档或 index 逐字节相同。无变化的 OCI 优化直接复制已验证归档，才能同时保留原始 config、manifest、index 和全部扩展字段。

### 构建工作状态与交付状态分开

`ImageConfig` 保存原始配置和初始化后的工作基线。`export()` 在工作状态未变时交付原始配置和原始字节；执行配置指令、追加 history/layer、消耗 ONBUILD 触发器等真实变更时，交付派生配置。阶段快照也保留原始字节。

仅 `FROM`、全局 ARG 选择基础镜像、无变化的 `FROM <前序阶段>` 不再无端改变 ImageID。新标签只改变归档引用。`source_date_epoch` 不重写无变化基础配置的时间字段。实际 COPY、ENV、RUN 等仍生成派生配置，不能拿旧 raw 配置强行保留旧 ID。

可选字段的 null 与缺省同样需要保留：`history:null` 在构建工作状态中补全为历史数组，`config.OnBuild:null` 视为没有触发器；仅 FROM 时不会把这些补全写回。实际派生时若基础层没有历史，内部使用 `imported base layer` 占位记录维护层对应关系，这些记录不宣称是原始构建命令。只读 `history` 和优化转存不会补造历史。

生产入口共用 `image_history(config, layer_count)` 校验可选历史和 `empty_layer` 类型；它只读配置。独立 conformance 读取器单独实现同样的规范检查，避免测试与被测路径共用错误逻辑。新增 `tests/test_optional_image_metadata.py` 的 12 项回归覆盖 null/缺省/空数组、无变化身份、真实 COPY/缓存命中、ONBUILD 消耗、裁层后导入与构建、发布前拒绝坏 history，以及独立读取器的空值兼容。

### 发布前验证绑定

CAS 导入 Docker archive 直接复用 reader 已验证的 config 字节，不重新打开输入获取另一份配置。OCI 导入再次消费原始 manifest/config 时核对其描述符摘要和大小，避免校验后输入被替换而发布混合数据。

CAS 的统一 ref 发布入口在落盘 blob 与 ref 之间核对 manifest/config/layer 元数据绑定，失败不发布引用。层在入库时已经校验，发布预检不重复完整哈希大层。

## 4. 全项目编码位置的判定

| 路径 / 模块 | 原始 JSON 如何处理 | 何时允许生成新 JSON |
| --- | --- | --- |
| `image_reader.py` | config 保存原字节和实际 digest；共同辅助函数验证 raw 与解析对象 | 新配置的明确编码、可信基础层缓存的本地元数据 |
| `registry.py` pull | 原始 config/manifest 保留；未压缩 OCI manifest 无变化时复用原字节 | 解压层导致描述符变化，或 Docker 媒体类型转 OCI；保留其他扩展字段 |
| `artifactory_download.py` | config blob 复制原字节；CAS 路径保存原始远端 manifest | Docker save 外层 manifest/repositories、旧式层 json；这些不是远端 OCI manifest |
| `cas_store.py` | 原始 config/manifest 入库；Docker 导出强制使用原始配置 | Docker archive 本来没有 OCI manifest，导入时合成一个；本地 ref 元数据 |
| `image_store.py` | 镜像复用检查实际 config digest；类型和层均一致的旧 tar 可从 CAS 修复 | base-map 本地路径映射；修复只替换 tar，保留 CAS 身份 |
| `image_cli.py` load/save/tag | config 和已有层保持原字节，inspect 使用真实 digest | 改标签重写 Docker save 外层引用；CLI 报告 |
| `builder.py` / `image_config.py` / `fast.py` | FROM 原始身份随阶段传递；Fast 使用同一 builder | 实际 Dockerfile 变化；Fast 配置文件、SBOM/provenance 等新产物 |
| `image_writer.py` | 转存 config 强制保留；层 tar 不重新打包 | Docker save manifest/repositories/旧式层 metadata、派生配置 |
| `oci_writer.py` | 转存 config 保留；已有 manifest 最终不变时保留 | 新布局 index、派生 manifest/config；模板保留扩展信息但不冒充旧 digest |
| `optimizer.py` | 未删除任何层：保留 config；OCI 同时原样保留 manifest/index | 确实裁层时更新 DiffID/history，生成新身份，并保留存活描述符和各级注解 |
| `rootfs_archive.py` / `image_changes.py` | export 只导出文件系统，不写 image config；flatten 从已验证镜像配置复制运行字段 | import 从 rootfs 创建新镜像；flatten 替换层链，明确使用 write_new，更新 history/DiffID/ImageID；不会承诺原身份 |
| `registry.py` push/push-index | config 上传原字节；零层 manifest 原字节；index 完全未变时原字节 | gzip 压缩、平台描述符或引用变化；保留注解，移除失效的旧 data/urls |
| `multiarch.py` | 子 config/manifest/layer blob 不重新编码；保留平台描述符额外注解 | 合并的顶层 index 是新对象；不自动合并两个来源顶层 index 的注解 |
| `cache.py` / `layer.py` | 缓存键针对明确的构建语义；不作为 ImageID | 指令缓存元数据、上下文指纹、缓存键稳定编码 |
| `hermetic.py` / `main.py` | 输入锁里的已有镜像身份来自实际文件/CAS 摘要 | 输入锁、报告、WSL 路径映射 |
| `attest.py` / `sbom.py` | 签名校验使用原始 DSSE payload/provenance 字节；subject 绑定实际归档 SHA-256 | 新 SBOM、provenance、信封的确定性编码；没有把它们的 ID 当成 ImageID |
| `conformance.py` / `progress/renderer_json.py` | 不负责转存现有身份 | 构造验收镜像、测试报告、进度事件 |

更新后全项目生产代码的实际镜像写入调用都明确选择了 `write_new` 新建或 `write_image` 转存接口；writer 内部的 `_write` 仅承担共用归档实现。

## 5. 本轮修复的剩余问题

| 修复前问题 | 现在的处理 |
| --- | --- |
| 没有裁层也改变 config/ImageID，OCI 注解和原始 manifest/index 丢失 | 无变化优化保留原始身份；实际裁层才生成新配置 |
| 仅 FROM、多阶段转接或内部缺省补全改变 ImageID | 保存初始化工作基线；输出未变的原始配置及字节 |
| Registry 未压缩 OCI manifest 重新编码 | 最终描述符不变时复用原始 manifest；转换时保留扩展元数据 |
| 零层 push 无故改变 manifest digest | 没有压缩变更，原字节上传 |
| push-index 无变化也重建 index，或丢失扩展字段 | 完全相同时复用 index raw；有变化时复制扩展字段后更新引用 |
| push、优化和多平台合并丢失部分描述符注解 | 保留注解及扩展字段；只移除不再对应新 blob 的内联 data/外部 urls |
| CAS 使用校验后的另一份 config，或发布不匹配 manifest/ref | 原始快照复用、再消费时核对描述符、统一发布预检 |
| OCI 校验后整份归档被另一合法镜像替换，自洽的新摘要绕过复核 | 校验返回原始 JSON 快照，重开归档逐字节核对，按快照绑定层摘要；失败不更新 ref |
| 布尔、整数、浮点和正负零被宽松比较混同 | 类型敏感的值比较；拒绝修改后的解析对象转存 |
| 新 config 可以编码 NaN/Infinity | 新 JSON 禁止这些非 JSON 常量；原始 blob 的共同解析辅助函数也拒绝它们 |
| 优化器对 `./` 归档成员解释与其他入口不同 | 共用规范化归档成员索引 |

## 6. 测试和迁移边界

新增 `tests/test_image_identity_contract.py` 的 21 项测试，样例包含非规范键顺序、缩进、末尾换行、中文转义、额外字段、布尔/整数/浮点和正负零。断言同时比较原始字节与 digest，不只比较 dict。

覆盖 Docker/OCI 无变化优化、实际裁层、FROM-only 与多阶段构建、缺省 history / config:null、真实 COPY 派生、load→tag→CAS→save、Artifactory 转换、真实本地 HTTP 拉取压缩/未压缩层→CAS→多阶段构建、零层 push、多平台 index、错误原始字节/声明摘要/对象变更、CAS 验证后的受控输入替换、签署和核对真实输出。

`tests/test_archive_verification_contract.py` 补充发布门禁测试，覆盖重复成员、链接层、异常 rootfs、JSON 上限、非 JSON 常量、合法归档整体替换、失败保留既有引用、原始字节快照、摘要正确但非 tar 的层，以及 GNU sparse 层的物理长度。完整回归的最新解释器和数量见 [开发与维护](../CONTRIBUTING.md)，下面的数字是首次专项审查记录。

首次专项测试的 13 项包含 15 个失败子样例和 7 个错误子样例；其中 4 个错误是计划新增的转存接口当时尚不存在，不能当作旧功能 bug 统计。随后增加贯通及真实变更测试，验证修复不强行保留已修改镜像的旧身份。

完整回归：267 项，264 通过、3 跳过；7 项离线 conformance 全部通过，92 个 Python 文件通过 Python 3.7 AST 语法解析。实际执行环境为 Windows / Python 3.12，未实际执行 Docker load、Linux RUN、Python 3.7 运行时或用户内网鉴权。

CAS schema 仍为 1。后续层结构审查将指令缓存版本更新为 8、可信基础层缓存版本更新为 2，旧条目自动重新构建或验证，无需清库或重新下载正常镜像。曾经被旧导出器重编码、且 CAS 仍保留正确原始配置的 tar 可以离线修复；如果旧归档/CAS 已经只剩错误重编码后的字节，无法从 dict 推回原始字节或原 ImageID，必须找回原始归档或重新拉取。

无变化 OCI 优化保留原归档，所以不会为了 `source_date_epoch` 重写其 tar header。真正的派生操作继续使用确定性编码。转存保持身份与新构建可重复，是两个独立契约。

本轮受控输入替换测试证明已修复的再读取问题；不代表所有文件操作对恶意宿主并发篡改、断电或强制退出具有事务保护。Registry push 仍会把未压缩层转为 gzip，因此普通有层镜像的远端 manifest digest 可能不同；不能把 config 保持 ImageID 宣称为整张远端 manifest 原样复制。
