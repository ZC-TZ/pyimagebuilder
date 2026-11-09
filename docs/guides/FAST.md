# Fast：WAR 与 dist.zip 打包

[文档目录](../README.md) · [项目首页](../../README.md) · [统一配置](CONFIGURATION.md)

Fast 适合已有 TongWeb/Tomcat 基础镜像的应用交付。已有模板可直接跳到[日常打包](#2-日常打包)。输入三个位置参数：**包路径、项目名、版本号**。无需编写 Dockerfile，也不需要 Docker。

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

UOS/Linux 可用相同参数创建本地 Tomcat 模板：

```bash
python3 main.py fast init --base-tar /data/base/tomcat-base.tar --base-image company/tomcat:9 --flavor tomcat --profile tomcat --output-dir /data/images
```

### 从 Artifactory 地址开始

没有基础 tar，但可以访问 nmkf 等镜像文件目录时：

```powershell
python .\main.py fast init `
  --base-url 'http://nmkf.zpk.abc/repo/tongweb:11' `
  --flavor tongweb --profile tongweb --username '仓库账号' `
  --server-config .\config\tongweb.xml --output-dir D:\images
```

init 不读取密码；日常打包前按[鉴权设置](CONFIGURATION.md#3-下载地址与鉴权)设置环境变量。地址是占位示例，需要替换为实际可下载目录。Fast 的 `baseUrl` 使用 Artifactory 下载路径；标准 Harbor/Registry 镜像推荐走 [普通 build](BUILD.md)。企业 HTTPS CA 可加 `--ca-file D:\certs\internal-ca.pem`。

直接编辑配置时，镜像引用与制品目录的标签写法不同。例如：

| 用途 | 示例 |
| --- | --- |
| `baseImage`，用于 FROM | `nmkf.zpk.abc/repo/tomcat:9` |
| `baseUrl`，实际制品目录 | `http://nmkf.zpk.abc/artifactory/repo/tomcat/9/` |

已使用 `镜像名:标签` 形式时不要再重复追加 `/9`；使用实际目录 URL 时，要进入标签对应目录。二者解析后的镜像引用必须一致。协议与证书见[配置说明](CONFIGURATION.md#3-下载地址与鉴权)。

## 2. 日常打包

先确认当前配置中已有对应 profile。以下示例均在工具目录运行，输出文件必须尚不存在。

**UOS / Linux（hisq 示例）：**WAR 命令按模板二选一，前端另用 dist.zip。

```bash
python3 main.py fast /data/packages/hisq.war hisq 1.0.0 --profile tongweb
python3 main.py fast /data/packages/hisq.war hisq 1.0.0 --profile tomcat
python3 main.py fast /data/packages/dist.zip hisq 1.0.0 --profile tomcat
```

**Windows / PowerShell（sfcx 示例）：**

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

在 UOS 上执行 `python3 main.py builds --project hisq`，去掉项目筛选可查看同一台账目录的全部记录。Fast 和普通 build 成功后均可登记；项目筛选只匹配 Fast 的项目字段。[台账指南](HISTORY.md)解释保存位置、字段、迁移与下载。

### 在浏览器里使用工作台

在 UOS 上执行 `python3 main.py --web`，Windows 使用 `python main.py --web`。在本机浏览器打开终端输出的完整会话链接。

工作台支持 Fast、普通构建、镜像库、任务、台账、配置与空间管理；应用包可直接填路径、选择文件或拖拽。[Web 指南](WEB.md)集中说明启动密码、文件输入、日志、任务复用和交付操作。

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
COPY --chown=tongadmin:tonggrp ["app.war", "/soft/TongWeb7.0/autodeploy/sfcx.war"]
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

首次从 Artifactory 拉取供构建使用时，先获取 manifest/config，随后每层下载完成便开始准备，
最多两层并行准备，与其余文件下载重叠；全部层通过校验并完成入库后才发布引用。
这一优化自动生效，不需要增加参数，也不要求清缓存。已经缓存的镜像仍直接复用。
首次下载的收益取决于网络、CPU 和磁盘是否有余量；不会跳过摘要、tar 结构或最终归档校验。
分阶段定位方法见[性能排查](PERFORMANCE.md)。若大量时间花在缓存读取和导出，可把 `cache.imageStore`、`cache.layerCache` 放到空间充足的本地 SSD，
并用相同输入对照阶段耗时；只修改缓存目录会让新目录首次使用时没有旧镜像。

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
