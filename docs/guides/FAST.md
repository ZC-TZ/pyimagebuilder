# Fast：WAR 与 dist.zip 打包

[文档目录](../README.md) · [项目首页](../../README.md) · [统一配置](CONFIGURATION.md)

Fast 适合已有 TongWeb/Tomcat 基础镜像的应用交付。输入三个位置参数：**包路径、项目名、版本号**。无需编写 Dockerfile，也不需要 Docker。

## 1. 首次配置基础镜像

以下两种方式选一种。`fast init` 只保存配置，不立即下载镜像；已有同名 profile 会报错。已有模板请直接编辑 `config.json`，不要反复执行 init。

### 从本地基础 tar 开始

在工具目录运行：

```powershell
python .\main.py fast init --base-tar D:\images\tongweb-base.tar `
  --base-image company/tongweb:11 --flavor tongweb --profile tongweb `
  --server-config .\config\tongweb.xml --output-dir D:\images
```

基础 tar 必须是 Docker save 镜像归档，且包含 `company/tongweb:11` 标签；将它替换为实际标签。`tongweb.xml` 必须是现有文件，可以改为你的部署配置路径。

Tomcat 模板可以另外创建：

```powershell
python .\main.py fast init --base-tar D:\images\tomcat-base.tar `
  --base-image company/tomcat:9 --flavor tomcat --profile tomcat `
  --output-dir D:\images
```

### 从 Artifactory 地址开始

没有基础 tar，但可以访问 nmkf 等镜像文件目录时：

```powershell
$env:PYIMAGEBUILDER_ARTIFACTORY_PASSWORD = '实际仓库密码'
python .\main.py fast init `
  --base-url 'https://nmkf.zpk.abc/repo/tongweb:11' `
  --flavor tongweb --profile tongweb --username '仓库账号' `
  --server-config .\config\tongweb.xml --output-dir D:\images
```

地址是占位示例，需要替换为实际可下载目录。Fast 的 `baseUrl` 使用 Artifactory 下载路径；标准 Harbor/Registry 镜像推荐走 [普通 build](BUILD.md)。企业 HTTPS CA 可加 `--ca-file D:\certs\internal-ca.pem`。

## 2. 日常打包

```powershell
python .\main.py fast D:\packages\report-1.2.3.war sfcx 1.3.4 --profile tongweb
python .\main.py fast D:\packages\dist.zip sfcx 1.3.4 --profile tongweb
```

| 输入 | 默认镜像标签 | 默认文件名 |
| --- | --- | --- |
| WAR | `sfcx_back:1.3.4` | `sfcx_back-1.3.4.tar` |
| dist.zip | `sfcx_front:1.3.4` | `sfcx_front-1.3.4.tar` |

输出目录取 profile 的 `outputDir`；未配置时放在应用包所在目录。项目和版本由位置参数决定，不从包文件名猜测。切换到 Tomcat 只需使用 `--profile tomcat`。

指定标签和输出文件：

```powershell
python .\main.py fast D:\packages\report.war sfcx 1.3.4 --profile tongweb `
  -t department/sfcx:1.3.4 -o D:\deliveries\sfcx.tar
```

指定另一份配置：

```powershell
python .\main.py fast D:\packages\report.war sfcx 1.3.4 `
  --config D:\builder-config\config.json --profile tongweb
```

独立脚本保留相同能力：把 `python .\main.py fast` 替换成 `python .\fast.py`，其后的参数保持不变，包括 `init`。

### 查询打包过的版本

Fast 和普通构建成功后会写入本机台账。以下以 UOS 上的 `hisq` 为例：

```bash
python3 ./main.py builds --config ./config.json --project hisq
python3 ./main.py builds --config ./config.json --tag hisq_front:1.0.0 --limit 5
```

列表从新到旧显示 UTC 完成时间、镜像标签、平台、构建方式和原输出路径。需要脚本处理时加 `--json`，可读取 Fast 项目名、版本、包类型、profile、归档大小和已计算的摘要。Docker 和 OCI 同时输出时只记一次打包，列出两个产物。

删除或移动 tar 不会删除台账条目。台账从启用此功能后开始积累，旧 tar 不会被自动登记；失败、`--check` 和 `--no-build-records` 不留记录。见[台账保存位置](CONFIGURATION.md#5-缓存与运行数据)。

### 在浏览器里使用工作台

```bash
python3 ./main.py --web
```

用 UOS 本机的浏览器打开终端输出的完整会话链接。默认端口是 8765；冲突时可加 `--port 0` 自动选择。原来的 `python3 ./main.py builds --web` 同样可用。

从主导航切换构建、任务、镜像库、打包台账、空间和配置。构建页顶部可切换快速打包、Dockerfile 构建、静态检查和 OCI 合并。切换时会保留当前页面中已填的表单内容。在「镜像库工具」中可按仓库、镜像归档、rootfs 文件系统和本地维护展开相应操作。

| 页面 | 操作 |
| --- | --- |
| 构建 | WAR/dist.zip 快速打包、Dockerfile 检查和构建、合并 amd64/arm64 OCI tar。高级选项包含输入锁、ARG、基础镜像映射、固定时间戳、SBOM 和签名 |
| 任务 | 自动刷新最近任务的状态概况和列表，支持按状态和关键词筛选，并显示耗时。详情可查看进度、导出当前会话日志、取消排队任务。重启后可查看结果、校验并下载产物或复用构建参数 |
| 镜像库 | 按标签、平台和来源筛选。支持拉取、导入、导出、校验和删除镜像引用，也可查看层历史、分析、优化、扁平化或导入和导出 rootfs |
| 打包台账 | 按项目或标签查全部历史；查看归档是否还在，有台账 SHA-256 时可校验并下载 |
| 空间 | 分开统计镜像库、构建缓存和交付 tar。支持勾选并清理旧镜像，以及确认后清理过期构建缓存 |
| 配置 | 查看运行位置，诊断已配置的仓库。支持新增和编辑 Tomcat/TongWeb 模板、测试基础镜像和预览配置差异 |

Fast 的应用包支持三种输入方式。可以填写运行工作台的机器上的文件路径，也可以用「选择文件」打开系统文件选择器，或把 WAR／dist.zip 拖到输入框。后两种方式会在本机暂存一份文件，单文件上限为 2 GiB。暂存文件在任务结束后清理，未提交的文件在 30 分钟后清理。复用任务时可重新选择应用包。

镜像库的导入、查看和校验 tar，多架构合并的两个 OCI tar，以及普通构建和静态检查的 Dockerfile 也支持系统选文件和拖拽。多架构合并会在提交前检查两个输入的架构。暂存 tar 的单文件上限为 2 GiB，Dockerfile 的上限为 1 MiB。复用曾经暂存 Dockerfile 的构建任务时，需要重新选择 Dockerfile。

「浏览本机」和「浏览目录」会列出运行工作台的机器上的路径，选中后直接读取原文件或目录。适合大归档、构建上下文、Fast 模板文件和签名私钥。输出 tar 可浏览目录并填入新文件名，系统会先检查是否已存在。

Fast 输出默认使用模板的 `outputDir`。模板没有配置输出目录时，手输路径会输出到应用包所在目录，选择或拖拽文件时需要填写输出 tar 路径。其他构建需填尚不存在的 tar 路径。仓库鉴权继续使用原配置与环境变量。模板在网页保存后即生效。手工编辑配置后重启工作台可刷新概览。测试本地基础 tar 会核对层。测试 Artifactory 只读取并校验远端 manifest 和 config，不下载层。

任务在一个工作台中按提交顺序执行。状态和可复用的构建表单保存在台账目录的 `web-jobs/`；构建参数值和签名密钥路径不会保存到复用表单。任务详情会自动跟随最新日志；向上滚动会暂停跟随，点击「跳到最新」或滚回底部即可恢复。原始日志只在本次会话内保留最近 250 条，可在详情页导出，分享前请检查命令输出。

成功后从台账下载时会先复制到临时文件，并核对副本的 SHA-256。下载结束或断开后释放副本。没有摘要的归档只显示位置。若移动或删除 tar，台账仍保留原始路径。

新版工作台也会在任务完成后登记归档和附属文件的路径、大小与 SHA-256。重启后从「任务」打开该任务，点「校验并下载」。登记的是文件记录，文件仍放在原输出目录。旧任务如果没有产物记录，可以在打包台账或原输出目录查找。

配置页和镜像库工具都有「仓库诊断」。选仓库后可检查 API 是否可访问，填写完整镜像引用还可检查标签与读取权限。诊断沿用配置文件、启动环境里的密码变量和 CA 设置。它只读取元数据，推送权限由实际推送检查。

#### 生成封闭构建输入锁

在「Dockerfile 构建」填写上下文和标签，展开高级选项，填写固定的 Unix 时间戳及尚不存在的输入锁 JSON 路径。JSON 要放在上下文目录之外。使用基础镜像时，提供本地基础 tar 或映射。从 `scratch` 构建时可留空。点「生成输入锁」，在成功的任务详情页选「使用此输入锁构建」，再填写输出 tar 路径。构建参数值需要按生成锁时的原值重新填写。封闭模式要求锁定的输入和工具链一致，且禁止执行 `RUN`。

#### 从产物直接交付

在打包台账或镜像库的条目旁点「产物操作」。网页会带入源归档、镜像标签和平台；历史条目提交后先核对台账中的 SHA-256，交付任务只读取校验通过的私有快照，任务运行期间需预留一份归档大小的临时空间。CAS 条目以选中时的 manifest 摘要为准，标签更新后需重新选择。

| 产物 | 可用操作 |
| --- | --- |
| Docker tar | 校验、导入、改标签并另存为新 tar、推送单平台镜像、导出 rootfs，以及层历史、分析、优化和扁平化 |
| 有台账 SHA-256 的 OCI tar | 校验、导入 CAS、推送单平台镜像、导出 rootfs，以及层历史、分析、优化和扁平化 |
| 镜像库中只有 CAS 数据的条目 | 校验引用和 blob、导出 Docker tar 或 rootfs，以及层历史、分析、优化和扁平化 |
| 多架构合并任务的 OCI tar | 在任务结果页点「校验并推送多架构镜像」，可校验归档或确认后推送 amd64 与 arm64 |

rootfs tar 只含合并后的文件系统。到「镜像库工具 → 从 rootfs 创建镜像」可将它打成 Docker 或 OCI 镜像，并按行填写 `ENV`、`USER`、`ENTRYPOINT` 等运行配置。导入支持本机路径、选择文件和拖拽，也接受 gzip、bzip2 或 xz 压缩的 rootfs tar。需要继承原镜像运行配置时，在「产物操作」使用「扁平化并另存」，必要时逐行填写要覆盖的配置。

签名构建完成后可在任务详情点击「校验交付签名」。从「产物操作」或「镜像库工具」也能进入验签，使用可信公钥校验 DSSE 签名、镜像归档及 SBOM 的摘要。

推送只接受 `config.json` 中已配置的 Registry / Harbor，目标必须明确写出标签。点「预览推送」会显示源归档和完整目标；点击「确认推送」后才会连接仓库。每次确认只能使用一次，检测到目标标签已存在时拒绝推送。账户和密码继续使用原有配置与环境变量；工作台不提供覆盖远端标签的开关。

空间页可以勾选旧镜像后批量删除。执行前会锁住所选引用，重新核对归档和引用身份。删除范围是勾选的引用及其镜像库归档，无引用层和交付 tar 仍提供独立预览。过期构建缓存清理会取得维护租约并重新比对候选文件。有同版本构建正在使用缓存、候选集合变化或确认过期时，清理会被拒绝。硬链接、压缩和共享数据可能使实际释放量与估算不同。

Linux 上可取消正在执行的任务；在 Windows 上，停止工作台时会等待正在执行的任务收尾。终端按 `Ctrl+C` 停止。会话链接每次启动生成，只应自己使用；服务只监听本机回环地址。

可加 `--project hisq` 或 `--tag hisq_front:1.0.0` 直接打开带初始筛选的历史页；`--limit 100` 限制历史页可浏览的最近匹配记录数。

## 3. 模板怎样影响 Dockerfile

| 设置 | TongWeb 默认值 | Tomcat 默认值 |
| --- | --- | --- |
| profile 名称 | `tongweb` | `tomcat` |
| 镜像内属主 | `tongadmin:tonggrp` | `tomcat:tomcat` |
| 部署目录 | `/soft/TongWeb7.0/autodeploy` | `/soft/tomcat/webapps` |
| 服务配置 | 可复制 `serverConfig` 到 `/soft/TongWeb7.0/conf/tongweb.xml` | 不使用 `serverConfig` |

这些目录和账户是模板默认值，必须符合实际基础镜像。可以在 profile 修改 `owner`、`deployDir`；具名属主需要基础镜像中存在相应账户。

以 TongWeb WAR 为例，生成的临时 Dockerfile 类似：

```dockerfile
FROM company/tongweb:11
COPY --chown=tongadmin:tonggrp app.war /soft/TongWeb7.0/autodeploy/sfcx.war
COPY --chown=tongadmin:tonggrp tongweb.xml /soft/TongWeb7.0/conf/tongweb.xml
```

`serverConfig` 未配置时没有第二条 COPY。WAR 在临时上下文中统一使用 `app.war`，部署时命名为 `<项目名>.war`。前端会先解开 ZIP，再将 dist 文件树复制到部署目录下的 `<项目名>f` 子目录（示例为 `sfcxf`）。ZIP 支持顶层 `dist/` 或直接包含前端文件；危险路径和超限归档会报错。前端 COPY 示例为 `COPY --chown=tongadmin:tonggrp dist /soft/TongWeb7.0/autodeploy/sfcxf`。

临时目录里的 `tongweb.xml` 是源配置文件的副本。源文件仍在 `config/tongweb.xml` 或配置指定的位置；临时目录在构建结束后清理。

## 4. 下载、缓存与交付

```text
profile → 基础镜像（本地 tar 或 Artifactory → CAS 镜像库）
        → 临时 Dockerfile / 应用文件 → COPY 层 → 新镜像归档
```

远端模板默认复用已经缓存的基础镜像；首次缺失才下载。普通 build 和 Fast 共用 `cache.imageStore`。构建路径直接消费 CAS，不必先包装成 base.tar。要刷新远端标签，使用 `main.py pull IMAGE --source artifactory`，并指定同一配置/镜像库。

验证结果后交付：

```powershell
python .\main.py verify D:\images\sfcx_back-1.3.4.tar
```

在目标 Docker 机器上执行 `docker load -i sfcx_back-1.3.4.tar`，再启动应用验收。

Fast 还支持 `--format docker|oci|both`、`--platform linux/amd64|linux/arm64`、`--cache-dir`、`--verify-cache`、`--workspace`、`--source-date-epoch` 和进度选项。Fast 不提供 build 的全部参数；需要 RUN、`--offline`、`--attest` 或自定义多阶段 Dockerfile 时，使用 [普通 build](BUILD.md)。

跨盘导出时，默认可在输出盘的私有目录生成并验证归档，再以硬链接发布，减少一次完整 tar 的搬运。
这要求输出目录在构建上下文外，且支持硬链接；不满足时继续使用原工作区。
想固定临时构建和归档写出位置时，显式指定 `--workspace /data/build-tmp`，该目录必须在构建上下文外。
机械硬盘或网络输出盘的读写表现可能不同，可与 `--workspace /tmp` 对照后选择；
独立 `fast.py` 和 `main.py fast` 都支持该参数。它不会修改镜像内容或缓存键。
