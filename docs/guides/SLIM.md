# 镜像裁剪与压缩

[文档目录](../README.md) · [命令速查](COMMANDS.md) · [Web 工作台](WEB.md)

`slim` 用于处理已经构建好的镜像：合并最终文件树、保留运行配置，按明确规则删除文件，并可压缩为 gzip。打包机器无需安装 Docker，Python 3.7 标准库即可运行。

## 1. 先选对操作

| 需要什么 | 使用什么 |
| --- | --- |
| 只给原 tar 包装 gzip，保留全部字节和镜像身份 | [compress](COMPRESSION.md) |
| 原样导出本工具镜像库中的镜像 | `save` |
| 合并所有层，保留最终可见文件和启动配置 | `flatten` |
| 合并为一层，同时裁剪文件、输出审计或 gzip | `slim` |
| 分析重复文件、大层及覆盖内容 | `analyze` |
| 在已有层结构上保守删除冗余整层 | `optimize` |

**slim 没有默认删除清单。** 不指定删除规则时只做扁平化；加 `--gzip` 再压缩。它不是给原 tar 简单套 gzip：会重建一层，产生派生镜像和新的 ImageID，继承 ENV、USER、WORKDIR、CMD、ENTRYPOINT 等运行配置。原始配置中的额外字段也保留；rootfs、构建历史和时间戳按派生镜像更新。

输入支持本地 Docker archive（含 gzip 包装）、未压缩的单平台 OCI archive，以及本工具已有的 CAS 镜像引用。压缩后的 OCI 归档需先解开外层 gzip 再裁剪。输出统一为 **Docker archive**：`.tar` 或 gzip 的 `.tar.gz` / `.tgz`。多平台 OCI index 应先选择并导出单个平台，不能用一次 slim 处理整个 index。

## 2. 最快使用

在包含 main.py 的工具目录执行。下面是 UOS/Linux 命令，Windows 将 `python3` 换为 `python` 并替换文件路径。

只合并并压缩，不主动删除文件：

```bash
python3 main.py slim /data/images/hisq_back-1.0.0.tar \
  -o /data/images/hisq_back-1.0.0-slim.tar.gz \
  --gzip --report /data/images/hisq_back-1.0.0-slim.json
```

输出必须是新文件，报告也不覆盖已有文件。省略 `-o` 时，本地输入旁生成 `原文件名-slim.tar`；加 `--gzip` 则为 `原文件名-slim.tar.gz`。显式输出为 `.tar.gz` / `.tgz` 时会自动启用 gzip；显式 `.tar` 配合 `--gzip` 会报错，避免后缀与内容不符。

交付到 Docker 机器后可以直接：

```text
docker load -i hisq_back-1.0.0-slim.tar.gz
```

Docker 官方支持从 gzip 压缩的 tar 加载镜像，见 [docker image load](https://docs.docker.com/reference/cli/docker/image/load/)。本工具会校验原始 Docker 归档，并从 gzip 结果重读，核对 CRC、长度和未压缩 SHA-256；实际应用还需在目标机器加载并启动验收。

## 3. 需要删除时，先预览

先查看候选删除项、被保护项和大文件，不写镜像：

```bash
python3 main.py slim /data/images/hisq_back-1.0.0.tar \
  --analyze-only --delete '/usr/share/doc' \
  --keep '/app/config' --report /data/images/hisq-plan.json
```

确认报告后，以同样规则写新镜像和另一份报告：

```bash
python3 main.py slim /data/images/hisq_back-1.0.0.tar \
  --delete '/usr/share/doc' --keep '/app/config' \
  -t hisq_back:1.0.0-slim \
  -o /data/images/hisq_back-1.0.0-slim.tar \
  --report /data/images/hisq-result.json
```

预览不是后续执行的授权凭据或固定计划。执行会重新读取输入、重新计算规则，期间应保持输入和规则文件不变。示例删除目录只是演示，不能据此认定业务不需要这些文件。

| 规则 | 语义 |
| --- | --- |
| `--delete '/path'` | 删除明确匹配的路径；目录匹配包含其后代 |
| `--keep '/path'` | 显式保护，优先于删除规则，同时保留必要父目录 |
| `--delete '*.log'` | 大小写敏感的通配匹配，`*` 可以跨越斜杠；不是 dockerignore 语法 |
| `--delete-from delete.txt` | UTF-8 / UTF-8 BOM，每行一条；忽略空行和以 # 开头的行 |
| `--keep-from keep.txt` | 同样格式的保护规则文件 |
| `--no-auto-protect` | 关闭内置保护，仍保留显式 keep；仅在了解后果时使用 |

delete/keep 及规则文件参数均可重复。镜像路径使用 `/`，不依赖宿主系统；禁止根目录规则、`..`、反斜杠与控制字符。规则匹配前会规范化重复斜杠和 `.`；规则文件总量有边界，不读取设备或管道。

默认自动保护账户/NSS/DNS、CA/TLS、时区等关键路径，并从镜像配置推导工作目录、卷、运行命令、HOME 和常见运行时目录。会跟踪镜像内的符号链接；保护到目录链接时会保守保护目标子树，硬链接按实际 inode 内容处理。

**自动保护是保守启发式规则，不是应用依赖扫描器。** 它无法证明 JNI、动态加载、脚本拼接路径或业务资源安全；需要你补 keep 并在目标环境验收。保护父目录不会自动保护所有兄弟文件。删除与保护的最终决定均可查完整 JSON 报告。

## 4. 压缩、大小与报告

| 参数/字段 | 含义 |
| --- | --- |
| `--compress-level 1..9` | 默认 6；1 更重视速度，9 更重视压缩率，实际收益取决于文件内容 |
| `--upload-limit BYTES` | 默认 2,000,000,000 字节，按**解压后的 Docker tar** 判断 |
| `--largest N` | 报告列出最大文件数量，默认 20，允许 1–1000 |
| `--workspace DIR` | 临时工作目录；别名 `--temp-dir` |
| `--source-date-epoch 0` | 固定派生镜像时间；也读取 SOURCE_DATE_EPOCH |
| `--report FILE.json` | 完整审计报告；省略时 CLI 仍将 JSON 写至 stdout |

`under_upload_limit` 只有在未压缩 tar **严格小于**限制时才为 true，达到或超出限制会输出警告，**不会使命令失败**。gzip 变小不能绕过解压后大小限制；此阈值也与 Web 上传暂存的 2 GiB 限制不同。

报告 schemaVersion 为 1，包含来源 ImageID/本地输入摘要、层数、删除/保护规则、逐路径决定及原因、前后大文件统计、硬链接去重后的载荷变化。实际构建后还包含派生 ImageID、产物 SHA-256、压缩前后大小和阈值判断。预览只有估计的载荷变化，不能精确判断输出 tar 大小，因此阈值结果为 null。

载荷减少不等于 tar 文件必然变小；归档头、填充、压缩和硬链接都会影响结果。没有提供 report 路径时，CLI 完整报告只在 stdout，需自行保存。Web 会自动另存完整 `.slim.json`，任务页面显示摘要。

处理时使用私有输入副本和临时层，不把镜像 rootfs 解压到宿主目录，也不执行镜像里的程序。需要预留输入、未压缩层、新 tar 和可选 gzip 的临时磁盘空间；Web 处理历史产物还会先建立已验证的来源副本。任一构建、校验、报告发布或临时清理失败，会回滚本次已发布的新文件；原归档始终不修改。

## 5. Web 中使用

从台账或镜像库进入“产物操作”，选择“裁剪与压缩镜像”：

1. 填写新的输出路径，以及可选的删除/保护规则。
2. 默认勾选“仅预览”，先提交并下载审计 JSON。
3. 查看删除项和保护原因后，返回同一来源，填写相同规则，取消“仅预览”。
4. 按需启用 gzip、选择压缩级别和上传限制，提交。
5. 在任务结果下载 tar/tar.gz 和 `.slim.json`。压缩输出填 `.tar` 时，网页会自动追加 `.gz`。

单平台 Docker、OCI 归档和镜像库 CAS 都可操作；多架构 OCI index 不提供此按钮。预览只登记报告，不登记不存在的 tar。不要把本功能当作应用功能测试。

## 6. 独立脚本与已有镜像库

以下三个入口调用同一实现：

```text
python main.py slim --help
python slim.py --help
python shrink_docker_archive.py --help
```

这里的 shrink_docker_archive.py 是项目内的兼容入口，需与项目模块一起分发；“独立运行”表示无需经过 main.py，不表示只复制一个文件即可工作。旧脚本常见的 `--image`、`--temp-dir` 别名保留，旧 `--workers` 等未实现参数会明确报错。

用已有 CAS 引用时：

```bash
python3 main.py slim hisq_back:1.0.0 --source local \
  --platform linux/amd64 --image-store /data/image-store \
  -o /data/images/hisq-cas-slim.tar
```

`--source` 选择镜像库来源，不会发起拉取；缺少基础镜像时先 pull/load。来源和平台应与镜像库记录一致。对多镜像归档，用 `--image 输入标签` 选择，`-t 新标签` 设置输出标签。

slim 不自动修改镜像库，也不写 build/fast 的成功打包台账。需要标签管理时另行 load；Web 的任务摘要会记录本次输出。身份与扁平化语义见 [rootfs 指南](../ROOTFS_COMMANDS.md)和[镜像身份契约](../IMAGE_IDENTITY.md)。
