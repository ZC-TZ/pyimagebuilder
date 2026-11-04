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

| 页面 | 操作 |
| --- | --- |
| 构建 | WAR/dist.zip 快速打包，自动预览标签、基础镜像、部署路径及输出冲突；Dockerfile 静态检查和普通构建 |
| 任务 | 查看排队、进度和日志；取消排队任务；重启后查看任务结果并复用构建参数 |
| 镜像库 | 列出本地镜像；拉取、导入、导出、检查、验证归档和查看占用；重复输入引用后删除指定本地镜像 |
| 打包台账 | 按项目或标签查全部历史；查看归档是否还在，有台账 SHA-256 时可校验并下载 |
| 空间 | 分开统计镜像库、构建缓存、已登记的交付 tar；预览候选项及估算大小 |
| 配置 | 查看配置路径、Fast 模板、仓库类型、镜像库和缓存位置 |

输入路径指运行程序的机器上的文件，网页不上传源文件。Fast 输出可留空，沿用模板的 `outputDir`，未设置时放在应用包所在目录；其他构建可填尚不存在的 tar 路径。仓库鉴权继续使用原配置与环境变量，网页不展示凭据；修改配置后重启工作台可刷新概览。

任务在一个工作台中按提交顺序执行。状态和可复用的构建表单保存在台账目录的 `web-jobs/`；原始日志只在本次会话内显示。成功后从台账下载时会先读取完整文件核对摘要；没有记录 SHA-256 的归档只显示位置。若移动或删除 tar，台账仍保留原始路径。

空间页只做预览。交付 tar 只统计台账中仍存在的文件，复用层、硬链接和文件系统可能使实际释放量与估算不同。执行批量清理前，应先停止共用镜像库或缓存的构建和拉取。

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

Fast 还支持 `--format docker|oci|both`、`--platform linux/amd64|linux/arm64`、`--cache-dir`、`--verify-cache`、`--source-date-epoch` 和进度选项。Fast 不提供 build 的全部参数；需要 RUN、`--offline`、`--attest` 或自定义多阶段 Dockerfile 时，使用 [普通 build](BUILD.md)。
