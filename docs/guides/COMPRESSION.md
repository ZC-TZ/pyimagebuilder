# 仅压缩 tar：保持镜像身份

[文档目录](../README.md) · [裁剪与压缩](SLIM.md) · [命令速查](COMMANDS.md)

`compress` 只把已有的未压缩 tar 包装为 gzip。它不重新打 tar、不合层、不解析和重新序列化镜像配置，也不修改标签或平台。

## 1. compress 和 slim 怎么选

| 操作 | 命令 | 镜像身份 |
| --- | --- | --- |
| 保持原 tar 全部字节，只减小传输包 | `compress` | 解码后 tar 逐字节一致，原 config/ImageID、层与 manifest 保留 |
| 合并最终文件树，可选裁剪和 gzip | `slim` | 重建单层和派生配置，产生新的 ImageID |
| 合并为一层，选择 Docker/OCI 格式 | `flatten` | 派生镜像，保留运行配置 |
| 保守删除冗余整层 | `optimize` | 是否改变身份取决于是否实际删除层 |

压缩包文件自身的 SHA-256 会改变，不能把它与原 tar 的 SHA-256 混为一谈。Registry 层 blob 的压缩方式也不受本命令影响：改变的是最外层归档包装。

## 2. UOS / Linux 最快使用

```bash
python3 main.py compress /data/images/hisq_back-1.0.0.tar
```

默认旁边生成 `hisq_back-1.0.0.tar.gz`，原文件保留；输出已存在时拒绝覆盖。指定路径、压缩级别和可选报告：

```bash
python3 main.py compress /data/images/hisq_back-1.0.0.tar \
  -o /data/delivery/hisq_back-1.0.0.tar.gz \
  --compress-level 1 \
  --report /data/delivery/hisq-compress.json
```

Windows 将 `python3` 换为 `python`，使用本机路径。两个入口参数相同：

```text
python main.py compress --help
python compress.py --help
```

独立脚本需与项目模块一起分发。这个命令只处理文件，不读取 config.json，不需要账户、Docker、WSL 或第三方依赖；支持 Python 3.7 及以上。

## 3. 输入、输出与校验边界

| 项目 | 行为 |
| --- | --- |
| 输入 | 本地未压缩 Docker save tar、单/多平台 OCI tar、rootfs tar；不要求选择单个标签或平台 |
| 输出 | 新的 `.tar.gz` 或 `.tgz`；不是 ZIP，也不生成派生镜像 |
| 已压缩输入 | 拒绝 gzip/bzip2/xz 等输入，避免误作二次压缩 |
| CAS 引用 | 不直接接受引用；先 save/cas export 为 tar，再压缩该 tar |
| stdin/stdout | 输入输出均使用文件路径；stdout 是 JSON 摘要，进度写 stderr |
| 镜像库和台账 | 不自动 load，不写 build/fast 成功台账；Web 会保存任务和产物记录 |

流程为：检查同一输入句柄的 tar 结构和成员元数据，流式压缩，同时计算原始及压缩字节摘要；再从 gzip 回读至 EOF，核对 CRC、长度及原始 SHA-256。源文件被替换或在读取期间变化时拒绝发布。

**结构检查不能证明镜像可运行或其内部 descriptor 正确。** 损坏的 tar 会被拒绝；格式上合法但语义不正确的镜像不能靠压缩修复。需要镜像验证时另用对应 verify/conformance 流程。

不解包 rootfs，也不另复制一份未压缩 tar；临时磁盘主要用于新压缩包。Web 的来源验证仍会先建立私有副本。新压缩包和报告处于同一发布事务，失败、取消或临时清理失败时回滚本次产物，不覆盖原 tar 或已有文件。

固定 gzip 时间与文件名字段，使相同输入、级别和压缩实现得到稳定结果；不承诺不同 zlib 版本产生相同压缩字节。压缩后可能变大，取决于原始数据是否已压缩，不保证压缩率。

## 4. 参数和报告

| 参数 | 默认 / 含义 |
| --- | --- |
| `-o / --output` | 输入名后追加 .gz；无 .tar 后缀时追加 .tar.gz |
| `--compress-level 1..9` | 默认 6；1 通常更快，9 通常压缩更充分 |
| `--report FILE.json` | 可选完整结果，报告必须是新 .json 文件 |
| `--upload-limit BYTES` | 默认 2,000,000,000 字节，按**原始 tar**判断，严格小于才通过 |
| `--workspace DIR` | 临时目录的父目录；别名 --temp-dir |
| `--progress auto/plain/json` | 终端进度形式；支持 --quiet、--no-color 和 --debug |

超过上传阈值仅输出警告，不使压缩失败。gzip 不能绕过“解压后 tar 大小”的限制；此阈值与 Web 上传暂存的 2 GiB 配额不同。

结果 schemaVersion 为 1，包含来源和输出路径、压缩级别、`source_sha256`、压缩包 `sha256`、前后大小、节省字节、`archive_bytes_preserved` 和上传阈值结果。不需要读取或改写所有配置 JSON 来推断这些结论：字节一致性校验直接保证归档内部内容保留。

在 Docker 机器上，Docker archive 的 gzip 包可按 [docker image load 文档](https://docs.docker.com/reference/cli/docker/image/load/)加载：

```text
docker load -i hisq_back-1.0.0.tar.gz
```

rootfs tar 压缩后仍是 rootfs，不能变成 docker load 镜像。OCI 包由支持该格式和压缩包装的工具导入，不能把全部 OCI 场景都当成 Docker archive。

签名若覆盖原 tar 的 SHA-256，不能直接拿 gzip 文件代替原文件验签；应按签名约定提供原 tar，或针对新的交付文件生成证明。镜像身份与归档摘要的区别见[身份契约](../IMAGE_IDENTITY.md)。

## 5. Web 操作

从台账或镜像库进入“产物操作”，选择“仅压缩归档”，填写新路径和压缩级别后提交。写成 .tar 时网页自动追加 .gz，实际输出旁边生成 `.compress.json`，两者都可在任务结果下载，重启后仍保留产物记录。

已有 Docker tar、单平台 OCI tar，以及多架构合并任务的 OCI tar 均可操作。仅 CAS 数据不显示该按钮，因为此时没有需要逐字节保持的源 tar；先导出再压缩。网页不会把“仅压缩”隐式替换成扁平化。
