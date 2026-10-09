# 进阶构建

[文档目录](../README.md) · [构建指南](BUILD.md) · [Dockerfile 兼容表](../DOCKERFILE_COMPAT.md)

普通应用包交付优先使用 Fast 或无 RUN 的 build。本页按需要单独查阅，各功能不必同时启用。

## 执行 RUN

RUN 会在基础镜像的 rootfs 中真正执行命令，并将文件系统差异制成层。需要 Linux namespace、OverlayFS、权限以及基础镜像里的可执行程序；Python 本身不提供这些内核能力。

在满足条件的 Linux 构建机上：

```bash
sudo python3 main.py build /data/project --base-tar /data/images/base.tar \
  -t company/app:1 -o /data/images/app.tar --run \
  --run-sandbox hardened --workspace /tmp
```

Windows 上已有可用 WSL 时：

```powershell
python .\main.py build D:\project --base-tar D:\images\base.tar `
  -t company/app:1 -o D:\images\app.tar --run --wsl --wsl-user root
```

WSL 内也要有 Python 及内核挂载能力。它不是自动安装步骤。默认网络为 none；RUN 需要下载软件时，可显式加 `--network host`。Dockerfile 的 `RUN --network=...` 还受执行策略限制，详情见兼容表。

| 沙箱 | 条件与边界 |
| --- | --- |
| `hardened`（默认） | Linux root、namespace/OverlayFS；收紧能力与部分系统调用 |
| `rootless` | 非 root 用户启动；单 UID/GID 映射，只适用 0:0 文件属主及相应基础镜像 |
| `legacy` | 可信构建显式选择的较少限制模式 |

rootless 不自动解决任意镜像权限；不能满足条件时会报错。执行模型与目标内核验收见 [架构说明](../ARCHITECTURE.md)，本机 Windows 尚未验证真实 Linux RUN。

### cache / secret mount

```dockerfile
FROM company/build-base:1
RUN --mount=type=cache,target=/root/.cache \
    --mount=type=secret,id=token,target=/run/secrets/token,required=true \
    /opt/build.sh
```

```bash
sudo python3 main.py build /data/project --base-tar /data/images/base.tar \
  -t company/app:1 -o /data/images/app.tar --run \
  --secret token=/data/secrets/token --cache-dir /data/builder-cache
```

secret 来自显式文件，不经 ARG 传递。临时挂载本身不进入 layer；命令若主动将秘密复制到其他路径，仍会留在结果中。支持的挂载选项见兼容表。

## 平台与双格式

无 RUN 的文件打包可在 Windows 上生成 amd64 或 arm64 镜像；基础镜像和最终目标平台必须一致。存在 RUN 时，跨架构执行需要宿主预先配置 QEMU/binfmt_misc，显式 `--allow-emulated-run` 才允许；工具不会自动安装这些组件。

```powershell
python .\main.py build D:\project --platform linux/arm64 `
  --base-tar D:\images\base-arm64.tar -t company/app:1 `
  -o D:\images\app-arm64.oci.tar --format oci
```

分别构建 amd64、arm64 的单平台 OCI 归档后，再合并：

```powershell
python .\main.py manifest create --amd64 D:\images\app-amd64.oci.tar `
  --arm64 D:\images\app-arm64.oci.tar -t company/app:1 -o D:\images\app-multi.oci.tar
```

合并命令不负责替你构建另一种架构。双平台 OCI 归档用 `manifest push` 推送，不等同于普通单平台 Docker save tar。

## 缓存与可重复构建

镜像库缓存 FROM，指令层缓存复用构建步骤；两者用途不同。`--no-cache` 不刷新 FROM，`--pull` 不关闭指令缓存。需要审查缓存完整内容时加 `--verify-cache`，清理方式见 [命令速查](COMMANDS.md#5-cas缓存与分析)。

build 可显式指定 `--source-date-epoch`，未指定时读取 `SOURCE_DATE_EPOCH`，再缺省为 0。Fast 的时间默认值为 0，建议同样显式传入。固定所有输入、参数和基础镜像身份后，可获得稳定产物；远端标签变化、网络下载和 RUN 中的非确定行为仍会改变结果。时间固定不能单独保证任意 Dockerfile 可重复。

## SBOM、provenance 与签名

build 的 `--attest` 生成 SPDX SBOM 与 SLSA 格式 provenance；`--sign-key` 同时生成离线 Ed25519 DSSE 签名，并隐含 attest。

先创建一次密钥，再构建：

```powershell
python .\attest.py keygen --private-key D:\keys\builder.key --public-key D:\keys\builder.pub
python .\main.py build D:\project --base-tar D:\images\base.tar `
  -t company/app:1 -o D:\images\app.tar --sign-key D:\keys\builder.key
```

生成以下附属文件：

```text
app.tar
app.tar.spdx.json
app.tar.provenance.json
app.tar.dsse.json
```

交付后核对：

```powershell
python .\attest.py verify --public-key D:\keys\builder.pub `
  --provenance D:\images\app.tar.provenance.json --envelope D:\images\app.tar.dsse.json `
  --sbom D:\images\app.tar.spdx.json --docker-archive D:\images\app.tar
```

生成双格式证明时，验证需同时提供被证明绑定的 Docker 与 OCI 文件。签名证明内容与密钥相符；SBOM 不等于完整依赖识别或漏洞扫描。证明是归档旁的附属文件，不会自动随普通 save/load 转移或成为 Registry referrer。

## 严格封闭构建

Hermetic 模式要求显式固定输入与时间，使用本地基础 tar/map，拒绝 RUN、网络和共享缓存等路径。启动 Python 前还必须设置 `PYTHONHASHSEED=0`。先生成输入锁，再按同一参数构建：

```powershell
$env:PYTHONHASHSEED = '0'
python .\hermetic.py --dockerfile D:\project\Dockerfile --context D:\project `
  --base-tar D:\images\base.tar --tag company/app:1 --source-date-epoch 0 `
  --output D:\images\inputs.lock.json
python .\main.py build D:\project --base-tar D:\images\base.tar `
  -t company/app:1 -o D:\images\app.tar --source-date-epoch 0 `
  --hermetic --input-lock D:\images\inputs.lock.json
```

锁绑定源码、上下文、基础镜像及构建选项；输入或程序变化后应重新生成锁。锁文件、输出与报告放在上下文外。该模式适用于当前限定的纯文件与元数据构建，不支持所有 Dockerfile。

## 进度与验收

build/pull/fast 等入口支持 `--progress plain` 或 `--progress json`；各子命令支持的细节参数以自身帮助为准。输出示例见 [构建进度](../PROGRESS.md)。

开发维护时可执行：

```powershell
python -m unittest discover -s tests
python .\conformance.py offline
```

离线检查不代替 Docker 对照、Linux RUN 或应用验收。目标环境可用 `conformance.py reference`、`compare`、`linux`，执行前查看相应 `--help`；测试方法和环境边界见 [开发与维护](../../CONTRIBUTING.md)。
