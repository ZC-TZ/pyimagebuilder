# 阶段技术记录（历史资料）

[文档目录](README.md) · [项目首页](../README.md)

本文保留历次 Phase 的技术说明与示例，便于追溯实现过程。部分早期章节中的“暂不支持”或命令示例已被后续阶段更新；**实际用法以 [当前用户指南](README.md#按任务阅读)、[Dockerfile 兼容表](DOCKERFILE_COMPAT.md) 和当前 CLI `--help` 为准**。

## 纯 Python 最小镜像构建器（Phase 16 实验版）

现代 Dockerfile 指令、标志的实际语义与剩余限制见 [`DOCKERFILE_COMPAT.md`](DOCKERFILE_COMPAT.md)。

## 统一构建进度

`main.py build`、`main.py fast`、独立的 `fast.py` 和 `main.py pull` 使用同一套构建事件。`[n/N]` 只计算实际参与构建的 Dockerfile 指令；基础镜像解析、准备 context、导出和计算归档摘要使用 `[+]` 阶段。长时间操作每约一秒报告存活状态；大文件下载、提取、COPY 和归档写入按字节显示进度。RUN 的 stdout、stderr 归属当前指令；失败显示 Dockerfile 行号和耗时。

成功时还会列出 Stage timings（秒）。Artifactory 路径分别统计目录/鉴权、大小探测、下载、层转换和基础 tar 打包；构建路径统计基础 tar 读取与索引、layer cache 查询/写入、COPY/ADD、Docker/OCI tar 写出与校验、最终归档哈希。fast 模式另统计 WAR/dist 准备。各阶段仅在实际执行时出现；并发下载显示的是该阶段墙上时间，不把每个线程耗时相加。使用 `--progress=json` 时，相同数据位于最终 `build_success.details.timings`。

性能优化：本工具自己下载并管理的基础镜像，其 layer 首次按 DiffID 完整校验后存入 `layerCache/base-layers/layers/<digest>.tar`；后续构建通过源归档与缓存文件的文件身份/大小/时间戳快速复用。显式传入的本地 `--base-tar`、`--base-map` 默认仍每次完整校验。普通指令层缓存首次校验后也使用文件身份快速命中，已有旧缓存会在下一次完整校验成功时升级元数据。`--verify-cache` 可强制每次完整哈希缓存层，并关闭基础层快速复用；缓存目录应放在仅受信任用户可写的位置。此模式不把时间戳视为抵御恶意篡改的安全证明。

Artifactory gzip 层转换会在同一次流式读取中计算压缩 blob digest 与解压后 DiffID，减少重复读盘；WAR fast 构建在同一文件系统优先使用硬链接暂存 WAR，无法创建硬链接时回退复制。`dist.zip` 仍需安全解包为目录，再按 Dockerfile 的 `COPY` 语义打包。

```powershell
python .\main.py build -t example/app:1 -o D:\images\app.tar --progress=plain D:\project
python .\main.py fast D:\packages\app.war sfcx 1.3.4 --profile tongweb --progress=json
python .\fast.py D:\packages\app.war sfcx 1.3.4 --quiet
python .\main.py pull registry.example.com/team/base:1 --progress=plain
```

`--progress=auto`（默认）在交互终端使用颜色与单行刷新，重定向或 CI 中自动采用逐行输出；`--progress=plain` 强制逐行，`--progress=json` 每行输出一个 JSON 事件。`--quiet` 只保留结果或错误，`--verbose` 增加细节，`--debug` 在错误时打印 Python traceback，`--no-color` 或 `NO_COLOR` 禁用颜色。纯 JSON 模式不混入普通文字。JSON 事件含 `type`、`elapsed`，步骤事件还含 `step`、`total`、`instruction`、`line`；消费者可使用 `progress.BuildReporter` 和自定义 renderer 接收同样的事件。示例见 [`PROGRESS.md`](PROGRESS.md)。

摘要中的 `Digest` 是最终输出 tar 文件的 SHA-256，用于交付文件校验；它不是 Registry manifest digest 或镜像 config digest。`--quiet` 的 JSON 输出只含最后一个成功或失败事件。网络进度取决于上游是否提供 blob 大小；未知大小时显示已下载字节数和存活心跳。

## 统一配置：config.json

所有入口使用同一份严格 JSON 配置；参考 [`config.example.json`](../config.example.json)。顶层 `cache` 管理镜像与层缓存，`repositories` 按主机名管理 Registry/Artifactory 来源、用户名、CA 与密码环境变量名，`fast.profiles` 保存 Tomcat/TongWeb 部署规则及基础镜像。**不在 JSON 中写密码或令牌**。`--config` 指定文件；未指定时先查 `PYIMAGEBUILDER_CONFIG`，再查当前目录 `config.json`，最后查脚本所在目录 `config.json`。显式指定的配置不存在会报错；普通构建在没有配置文件时仍可通过命令行参数运行。配置中的相对路径按配置文件所在目录解析。命令行参数优先于配置值。

```json
{
  "schemaVersion": 2,
  "cache": {"imageStore": "./data/image-store", "layerCache": "./cache/layers"},
  "repositories": {
    "nmkf.zpk.abc": {"source": "artifactory", "username": "robot-account",
                     "passwordEnv": "PYIMAGEBUILDER_ARTIFACTORY_PASSWORD"}
  },
  "fast": {"defaultProfile": "tongweb", "profiles": {
    "tongweb": {"flavor": "tongweb", "baseImage": "nmkf.zpk.abc/repo/tongweb:11",
                  "baseUrl": "https://nmkf.zpk.abc/repo/tongweb:11"},
    "tomcat": {"flavor": "tomcat", "baseImage": "nmkf.zpk.abc/repo/tomcat:9",
                "baseUrl": "https://nmkf.zpk.abc/repo/tomcat:9"}
  }}
}
```

`fast init` 会向配置文件添加一个 profile，并在用 `--base-url` 时写入对应主机的仓库设置；也可以手工编辑 JSON。示例镜像名和 URL 都是占位值，需换成真实基础镜像。默认使用 `tongweb`；要用 Tomcat，传 `--profile tomcat`。`fast.json` 顶层旧格式不再使用。

示例中的 `fast.profiles.tongweb.serverConfig` 指向随工具提供的 `./config/tongweb.xml`；Fast 构建会把它复制到镜像内 `/soft/TongWeb7.0/conf/tongweb.xml`。私有 CA 文件仍是可选项，只有文件确实存在时才添加 `repositories.<主机>.caFile`。相对路径按 `config.json` 所在目录解析；若把配置文件移到别处，也要调整 XML 路径或一并复制 `config/` 目录。

## Fast 模式：应用包、项目名、版本号

第一次需要配置基础镜像来源与部署规则；之后按旧脚本习惯明确传入**应用包、项目名、版本号**。统一入口是 `main.py fast`，独立入口 `fast.py` 继续可用；两者调用同一套逻辑。Fast 模式不会调用 Docker/Podman，也不会运行 Dockerfile `RUN`。它用本工具生成 Dockerfile 与临时 context，调用内部构建器直接输出可 `docker load` 的 tar。基础镜像可以是现成本地 Docker save tar，也可以是旧下载脚本支持的 Artifactory 制品目录或镜像引用；后一种在首次构建时自动下载、校验、转换并缓存。它简化操作，但仍需读取、校验和重写镜像归档，耗时取决于基础镜像大小、网络和磁盘速度。

```powershell
# 只做一次；base-image 须与基础 tar 中的镜像引用对应
python .\main.py fast init --base-tar D:\images\tomcat.tar `
  --base-image mycompany/tomcat:9 --flavor tomcat --profile tomcat `
  --output-dir D:\images

# 后续明确传包路径、项目名、版本号；默认从当前目录的 config.json 读取配置
python .\main.py fast D:\packages\report-1.2.3.war sfcx 1.3.4 --profile tomcat
python .\main.py fast D:\packages\report\dist.zip sfcx 1.3.4 --profile tomcat

# 独立入口仍可直接使用，参数与 main.py fast 后面的参数完全相同
python .\fast.py D:\packages\report-1.2.3.war sfcx 1.3.4 --profile tomcat
```

若基础镜像位于截图中的 Artifactory，可只配置一次完整镜像引用。无协议前缀时默认尝试 HTTPS；若该内网服务仅提供 HTTP，必须显式写 `http://`。URL 会按旧下载程序的规则转换为 `/artifactory/<仓库>/<路径>/<tag>/` 目录。首次构建下载 Registry manifest、config 和 blob，校验 SHA-256 与 diff ID，生成基础 Docker tar；后续使用 `cache.imageStore` 指定的目录（未设置时为配置目录下 `.fast-base-cache`）中的归档，不重复访问服务。缓存中基础镜像不会自动随远端 tag 更新；需要更新时删除对应缓存文件后重新构建，或用新的 tag 配置新 profile。

```powershell
$env:PYIMAGEBUILDER_ARTIFACTORY_PASSWORD = '从受控渠道取得的密码'
python .\fast.py init --base-url 'https://nmkf.zpk.abc/<仓库>/<镜像>:<tag>' `
  --flavor tongweb --profile tongweb --username '你的仓库账号' `
  --server-config D:\config\tongweb.xml --config D:\config\config.json
python .\fast.py D:\packages\report-1.2.3.war sfcx 1.3.4 `
  --config D:\config\config.json --profile tongweb
```

密码只从 `PYIMAGEBUILDER_ARTIFACTORY_PASSWORD` 环境变量读取，不写入配置或命令行；可用 `--password-env` 改变量名，私有 HTTPS CA 用 `--ca-file`。未设置用户名与密码时按匿名访问处理。当前支持同源 HTTP(S) Basic 鉴权，跨主机重定向会拒绝，避免把凭据带往其他站点；若内网实际鉴权是令牌、单点登录或另一个域名的下载地址，需按服务响应调整鉴权适配层。与旧脚本一样，Artifactory 目录方式只支持单平台 schema 2 / OCI image manifest；多平台 manifest list 会报错。`registry.py pull` 仍用于标准 Registry V2/Harbor 接口。

以上 WAR 命令生成 `sfcx_back:1.3.4`、`sfcx_back-1.3.4.tar`；前端生成 `sfcx_front:1.3.4`、`sfcx_front-1.3.4.tar`。包文件名不会决定项目名或版本。可用 `-t/--tag`、`-o/--output` 覆盖默认标签和 tar 路径；`--config` 可选择配置文件，`--profile` 可选择 Fast 配置档。输出路径已存在时拒绝覆盖。

Tomcat 模板与旧脚本一致：WAR 放到 `/soft/tomcat/webapps/<项目>.war`，前端目录放到 `/soft/tomcat/webapps/<项目>f`，默认属主 `tomcat:tomcat`。TongWeb 模板的部署目录是 `/soft/TongWeb7.0/autodeploy`，默认属主 `tongadmin:tonggrp`。初始化时可覆盖 `--owner`、`--deploy-dir`；TongWeb 的 `--server-config path/to/tongweb.xml` 可额外复制到 `/soft/TongWeb7.0/conf/tongweb.xml`。不会自行生成或覆盖服务器配置。示例：

```powershell
python .\fast.py init --base-tar D:\images\tongweb.tar `
  --base-image mycompany/tongweb:11 --flavor tongweb --profile local-tongweb `
  --server-config D:\config\tongweb.xml --config D:\config\config.json
python .\fast.py D:\packages\app.war sfcx 1.3.4 --config D:\config\config.json --profile local-tongweb
```

前端 ZIP 可在根目录直接放静态文件，也可包含顶层 `dist/` 目录；工具会安全解压到临时 context，拒绝越界路径、链接与特殊文件。基础镜像须已有运行环境和启动配置。若需要额外 `RUN`、复杂 Dockerfile、多阶段构建或严格输入锁，请使用 `main.py`。

## Docker 风格命令行：build、pull 与 fast

新入口接受位置式 build context，默认读取 `<context>/Dockerfile`。`build` 子命令、`-f/--file`、`-t/--tag`、`-o/--output` 和 `--network` 与 Docker 的常见写法相近；原有 `--dockerfile --context --tag --output` 仍可用。

```powershell
python .\main.py build -f D:\project\Dockerfile -t mycompany/myapp:1.0 `
  -o D:\images\output-image.tar --base-tar D:\images\base-image.tar D:\project
```

如果 Dockerfile 位于 context 根目录，可省略 `-f`。`-o` 在本工具中直接指定**本地镜像 tar 文件**，并非 Docker Buildx 的 `--output type=...,dest=...` 格式；由于没有 Docker daemon，不能省略输出后期望镜像进入本地 Docker 镜像库。`--base-tar` / `--base-map` 可显式选择离线基础镜像；如果都不提供，`build` 会按 Dockerfile 中实际使用的 `FROM` 查找本地镜像缓存，缺失时自动拉取。仅支持本地目录 context，不支持 Docker 的 URL 或标准输入 context。当前 `-t` 要求显式 `repository:tag`。

`main.py pull` 将基础镜像下载为 Docker tar 并放入本工具的本地镜像缓存；`-o` 可同时复制一份到指定路径。标准 Registry V2/Harbor 默认用 `--source registry`；旧下载脚本使用的 Artifactory 制品目录选择 `--source artifactory`。两者都是纯 Python，不需要 Docker daemon。例子：

```powershell
$env:PYIMAGEBUILDER_ARTIFACTORY_PASSWORD = '从受控渠道取得的密码'
python .\main.py pull 'nmkf.zpk.abc/<仓库>/<镜像>:<tag>' `
  --source artifactory --username '仓库账号' -o D:\images\base.tar

# 也可以让 build 在本地缓存没有基础镜像时自动拉取
python .\main.py build -f D:\project\Dockerfile -t sfcx_back:1.3.4 `
  -o D:\images\sfcx_back.tar --pull-source artifactory `
  --username '仓库账号' D:\project
```

`build --pull` 强制刷新所需基础镜像；`build --offline` 只使用已缓存镜像或显式 `--base-tar/--base-map`，缺失时立即失败。缓存默认位于系统临时目录的 `pyimagebuilder-image-store`，用 `--image-store` 指定持久目录。`pull` 每次主动刷新缓存；普通 `build` 命中缓存时不会访问仓库。Artifactory 无协议镜像引用默认尝试 HTTPS，服务只提供 HTTP 时需显式加 `--insecure-http`；标准 Registry HTTP 也需要显式开启该选项。认证密码分别从 `PYIMAGEBUILDER_ARTIFACTORY_PASSWORD` 或 `PYIMAGEBUILDER_REGISTRY_PASSWORD` 环境变量读取，`--password-env` 可改名，私有 CA 用 `--ca-file`。`FROM scratch` 无需拉取。`--hermetic` 严格模式仍不会联网。

当前自动拉取要求 `FROM` 写完整的 `主机/仓库/镜像:标签`，不实现 Docker Hub 的短名补全，也不对 Artifactory 多平台 manifest list 自动选择平台。显式 `--base-tar/--base-map` 的构建不会访问远端；`--pull` 与这些本地覆盖参数同时使用会报错。`main.py pull` 与真正的 `docker pull` 也有一处区别：它把结果存为本地 Docker tar 缓存，不会进入 Docker daemon 的镜像库。

### 镜像检查与交付命令

`main.py` 还提供以下命令。`inspect`、`verify`、`history`、`tag` 读取本地 Docker save tar；`images` 只列出本工具的基础镜像缓存，不列 Docker daemon 中的镜像。`verify` 校验归档 config 摘要、各层 DiffID 与平台信息，并输出整个 tar 的 SHA-256；它不代替目标机器上的启动验证，也不校验签名（签名使用 `attest.py verify`）。

```powershell
python .\main.py inspect D:\images\sfcx_back-1.3.4.tar
python .\main.py verify D:\images\sfcx_back-1.3.4.tar
python .\main.py history D:\images\sfcx_back-1.3.4.tar
python .\main.py images --image-store D:\images\pyimagebuilder-store
python .\main.py load -i D:\images\base.tar --image-store D:\images\pyimagebuilder-store
python .\main.py save nmkf.zpk.abc/project/base:1 -o D:\images\base-copy.tar `
  --image-store D:\images\pyimagebuilder-store
python .\main.py rmi nmkf.zpk.abc/project/base:1 --image-store D:\images\pyimagebuilder-store
python .\main.py build D:\project --check
python .\main.py system df --image-store D:\images\pyimagebuilder-store `
  --cache-dir D:\images\pyimagebuilder-cache
python .\main.py image prune --older-than 30 --dry-run `
  --image-store D:\images\pyimagebuilder-store
python .\main.py tag D:\images\sfcx_back-1.3.4.tar `
  nmkf.zpk.abc/project/sfcx_back:1.3.4 -o D:\images\sfcx_back-registry.tar
python .\main.py cache ls --cache-dir D:\images\pyimagebuilder-cache
python .\main.py cache prune --cache-dir D:\images\pyimagebuilder-cache
python .\main.py cache prune --cache-dir D:\images\pyimagebuilder-cache `
  --older-than 30 --dry-run
python .\main.py analyze --archive D:\images\sfcx_back-1.3.4.tar
python .\main.py optimize --archive D:\images\sfcx_back-1.3.4.tar `
  --output D:\images\sfcx_back-optimized.tar
```

`load` 校验 Docker save tar 后存入 PyImageBuilder 自己的镜像库；`save` 从该库复制出 Docker tar，`rmi` 删除该库里的归档。它们不操作 Docker daemon。多标签 tar 导入时需用 `--tag` 指明要索引的标签；当前存储单位仍是整个 tar，`rmi` 会删除该归档，而 `images` 会列出归档里的全部标签。同一标签与平台在本地已存在不同内容时需显式 `--replace`。已导入的镜像可由 `build --offline` 按完整 `FROM` 引用使用，普通构建优先使用导入的本地镜像；`build --pull` 仍强制访问远端。若同一标签同时有本地导入和 Registry/Artifactory 缓存，`save/rmi` 需用 `--source` 消除歧义。

`build --check` 只做静态语法、目标阶段、`.dockerignore` 限制及可确定的本地 COPY/ADD 来源检查，不要求 `-t/-o`；它不检查基础镜像是否可下载、变量展开后的来源、远程 ADD 或 RUN 的运行效果。`image prune` 必须指定 `--older-than DAYS`，按归档文件修改时间筛选；`--dry-run` 先看候选，执行时会删除符合条件的**有标签镜像**，使用期间不要并发构建。`system df` 统计本工具镜像库和层缓存占用。`cache prune` 默认仅删除损坏条目和孤立层；带 `--older-than DAYS` 时会按缓存条目文件修改时间删除旧条目，再清理失去引用的层。`tag` 写入新的 tar，不覆盖原文件或现有输出；目前要求单镜像 Docker archive。`inspect`、`verify`、`history` 在多镜像或多标签归档上使用 `--tag` 指定对象。

标准 Registry V2/Harbor 的远端 manifest 与上传命令如下。密码只从环境变量读取；若服务需要私有 CA，可加 `--ca-file`。`manifest inspect` 获取 manifest/index 和 digest，不下载各层。Artifactory 的旧制品目录 URL 不属于此命令支持的标准 Registry V2 接口。

```powershell
$env:PYIMAGEBUILDER_REGISTRY_PASSWORD = '从受控渠道取得的密码'
python .\main.py manifest inspect nmkf.zpk.abc/project/sfcx_back:1.3.4 `
  --username '仓库账号'
python .\main.py push D:\images\sfcx_back-registry.tar `
  nmkf.zpk.abc/project/sfcx_back:1.3.4 --username '仓库账号'
python .\main.py manifest create --amd64 D:\images\app-amd64.oci.tar `
  --arm64 D:\images\app-arm64.oci.tar `
  -t nmkf.zpk.abc/project/app:1.0 -o D:\images\app-multi.oci.tar
python .\main.py manifest push D:\images\app-multi.oci.tar `
  nmkf.zpk.abc/project/app:1.0 --username '仓库账号'
```

`manifest create` 合并已验证的 amd64/arm64 单平台 OCI tar；`manifest push` 将双平台 OCI index 发布到标准 Registry V2/Harbor。`push` 和 `manifest push` 默认拒绝覆盖远端已有 tag；确认需要替换时加 `--overwrite`。原有的 `registry.py pull/push/push-index`、`multiarch.py`、`optimizer.py analyze/optimize/cache`、`attest.py keygen/verify` 均保持独立运行。`image_cli.py` 也能独立运行，参数是上述 `main.py` 命令去掉 `main.py` 后的部分，例如 `python .\image_cli.py load -i D:\images\app.tar`。

构建不依赖已有镜像构建器或容器运行时。输入 Dockerfile 与 build context；基础镜像可由本地 Docker save tar 提供，也可按 `FROM` 拉取（`FROM scratch` 无需基础镜像）。输出新的 Docker save tar。Phase 12 支持 linux/amd64 与 linux/arm64，并可合并双平台 OCI index。当前最低目标为 Python 3.7，版本差异由 `compat.py` 处理。无 `RUN` 的构建可在 Windows 原生运行；含 `RUN` 的构建需要 Linux 内核、可用的 namespace 与 OverlayFS。默认 `hardened` 模式仍需 root；受限的 `rootless` 模式需要允许非特权用户命名空间。Windows 可显式加 `--wsl`，让 WSL 中的 Linux Python 执行同一工程代码。

```powershell
python .\main.py `
  --dockerfile D:\project\Dockerfile `
  --context D:\project `
  --base-tar D:\images\base-image.tar `
  --tag mycompany/myapp:1.0 `
  --output D:\images\output-image.tar
```

Dockerfile 示例：

```dockerfile
FROM mycompany/tomcat:9
ENV APP_MODE=production
WORKDIR /usr/local/tomcat
COPY app.war webapps/ROOT.war
CMD ["catalina.sh", "run"]
```

Phase 3 的本地文件指令示例：

```dockerfile
FROM mycompany/base:1
COPY --chown=1000:1000 --chmod=0750 start.sh /app/start.sh
ADD bundle.tar.gz /opt/assets/
ADD dist.zip /opt/frontend/
USER 1000:1000
SHELL ["/bin/sh", "-c"]
EXPOSE 8080 53/udp
LABEL service=example owner="Ops Team"
VOLUME ["/data", "/var/log/app"]
```

本地 `ADD` 按**文件内容**识别 tar，并展开未压缩、gzip、bzip2、xz 格式；普通 `dist.zip` 仍作为一个文件复制。`--chown` 支持数字或镜像内 `/etc/passwd`、`/etc/group` 的名称；单独指定用户时，GID 取相同数字。`--chmod` 当前只支持三或四位八进制，符号式权限会明确报错。`COPY` 和 `ADD` 保留目录、普通文件、符号链接与可识别的硬链接，拒绝越界及 OCI 保留的 `.wh.*` 路径；新层成员的修改时间按 Phase 8 的固定时间戳写入。zstd tar、远程 `ADD`、递归 `**` glob、经过构建目录符号链接的源路径仍不支持。

`EXPOSE` 支持单个端口及 `tcp/udp/sctp` 协议，默认 TCP；`LABEL` 写入镜像标签；`VOLUME` 接受绝对路径，会创建缺失目录并写入镜像配置。Phase 4 还把临时构建目录放到 context 外，避免 `COPY .` 把临时基础镜像和中间层复制进去。若 context 中的 `.dockerignore` 含有效规则，当前版本会明确失败，避免忽略过滤规则后产出错误镜像。

若有多个基础镜像，使用 JSON 映射代替 `--base-tar`：

```json
{
  "mycompany/tomcat:9": "D:/images/tomcat9.tar",
  "mycompany/nginx:1.27": "D:/images/nginx.tar"
}
```

```powershell
python .\main.py --dockerfile D:\project\Dockerfile --context D:\project `
  --base-map D:\images\base-map.json --tag mycompany/myapp:1.0 `
  --output D:\images\output-image.tar
```

## 执行 RUN

Linux/UOS 上：

```bash
python3 main.py --dockerfile /data/project/Dockerfile --context /data/project \
  --base-tar /data/images/base-image.tar --tag mycompany/myapp:1.0 \
  --output /data/images/output-image.tar --run --workspace /tmp
```

Windows 上切换到**已安装 Linux 发行版且有 Python 3 的 WSL**：

```powershell
python .\main.py --dockerfile D:\project\Dockerfile --context D:\project `
  --base-tar D:\images\base-image.tar --tag mycompany/myapp:1.0 `
  --output D:\images\output-image.tar --run --wsl
```

Windows 桥接在 `legacy`、`hardened` 模式下默认使用 `wsl.exe --user root --exec python3`；在 `rootless` 模式下使用 WSL 默认用户，也可用 `--wsl-user` 指定非 root 用户。它把 `C:\...` 映射为 `/mnt/c/...`。如需指定发行版、Linux Python 或挂载前缀，可用 `--wsl-distro`、`--wsl-python`、`--wsl-mount-root`。WSL 中的临时 rootfs 默认放在 Linux `/tmp`；不要把 OverlayFS 的 upper/work 放在 `/mnt/c` 等 Windows 挂载盘。`--base-map` 中的 Windows 路径会自动转换为 WSL 路径。

`RUN` 默认无网络隔离外的联网能力；需要访问内网软件源时可显式使用 `--run-network host`，但基础镜像还需有可用 DNS 配置与对应包管理程序。每条 `RUN` 真正在基础镜像 rootfs 中执行；返回非零则构建失败。当前支持 `USER` 与 `SHELL` 状态传递，文件变化经 OverlayFS upperdir 转换为新层。

## Phase 5：持久化 Layer Cache

命令行默认启用跨构建缓存，目录是运行该 Python 的系统临时目录下的 `pyimagebuilder-layer-cache`。在 Windows 使用 `--wsl` 时，默认放在 WSL Linux 的 `/tmp/pyimagebuilder-layer-cache`。建议在内网机器上明确指定持久目录，避免系统定期清理临时目录：

```bash
python3 main.py --dockerfile /data/project/Dockerfile --context /data/project \
  --base-tar /data/images/base-image.tar --tag mycompany/myapp:1.0 \
  --output /data/images/output-image-v2.tar --run \
  --cache-dir /data/pyimagebuilder-cache
```

`--cache-dir` 必须在 build context 外；`--no-cache` 同时关闭缓存读取和写入。首次构建产生的 `COPY`、`ADD`、`WORKDIR`、`VOLUME`、`RUN` 层会以内容摘要存入缓存，后续命中时复用。尤其 `RUN` 命中时无需重新执行命令，也无需为它创建 OverlayFS 执行环境。命令末尾会打印 hit/miss/store 数量。Windows 原生入口若某个 `RUN` 已命中缓存，可直接复用；一旦未命中，实际执行仍需 Linux/WSL。

缓存键沿 Dockerfile 顺序推进，包含基础镜像 config 与 diff IDs、之前的指令及其结果、当前指令；`COPY/ADD` 还校验展开后的源文件内容、路径和元数据。即便文件大小和修改时间不变，只要内容改变，相关层与后续层也会失效。损坏的缓存层会当作未命中并重新生成。`RUN` 使用网络时，外部仓库内容变化本身不会自动使缓存失效；需要更新时用 `--no-cache` 或修改相关 Dockerfile 指令。缓存包含构建产物的完整层内容，应放在受控目录。

缓存主要节省重复的文件指令和 `RUN`。为校验输入并生成独立的 `docker load` tar，构建仍会读取基础镜像、检查缓存层摘要并重新写出最终归档；因此耗时改善取决于 Dockerfile 中实际命中的工作量。不同机器之间可复制缓存目录，但目录是普通文件存储，不依赖 Docker 或 Podman。

## Phase 6：多阶段构建

支持多条 `FROM`、`AS` 阶段名、按零起始序号引用前序阶段、`FROM <前序阶段>`、`COPY --from=<前序阶段>`，以及不需要基础镜像 tar 的 `FROM scratch`。默认只输出最后一个阶段；中间阶段的构建工具、依赖与层不会被自动加入最终阶段。阶段名不区分大小写，`COPY --from` 的来源路径始终从该阶段 rootfs 的 `/` 解析，与该阶段的 `WORKDIR` 无关。

```dockerfile
FROM mycompany/build-base:1 AS build
WORKDIR /src
COPY . /src/
RUN ./build.sh && mkdir -p /out && cp app.war /out/app.war

FROM mycompany/runtime-base:1 AS runtime
COPY --from=build /out/app.war /app/app.war
CMD ["java", "-jar", "/app/app.war"]
```

不同外部 `FROM` 使用 `--base-map` 显式映射到内网已有的 Docker save tar：

```json
{
  "mycompany/build-base:1": "/data/images/build-base.tar",
  "mycompany/runtime-base:1": "/data/images/runtime-base.tar"
}
```

```bash
python3 main.py --dockerfile /data/project/Dockerfile --context /data/project \
  --base-map /data/images/base-map.json --tag mycompany/app:1 \
  --output /data/images/app.tar --run --cache-dir /data/pyimagebuilder-cache
```

`--target build` 或 `--target 0` 可以输出指定阶段，并跳过后续阶段的执行。只用 `FROM scratch` 或已有阶段的 Dockerfile 可省略 `--base-tar/--base-map`。`COPY --from` 支持文件、目录、多源、简单 glob，以及 `--chown/--chmod`；本地 `ADD` 不支持 `--from`。`FROM --platform=linux/amd64|linux/arm64` 可覆盖该阶段的平台；跨平台阶段间 `COPY --from` 仅复制文件，不转换可执行文件架构。Phase 13 支持下述受限 `ARG`/变量展开。当前 `COPY --from=<外部镜像>`、并行阶段构建仍不支持，会明确报错；外部镜像请先通过 `FROM` 定义为阶段。与前面各阶段一样，真实 Linux `RUN` 与最终目标环境的 `docker load` 仍需在目标机验证。

## Phase 7：Docker archive 与 OCI Image Layout

`--format docker` 是默认值，`--output` 得到现有的 Docker save tar。`--format oci` 让同一个 `--output` 成为 OCI Image Layout tar。`--format both` 一次构建生成两份已校验的归档：`--output` 是 Docker tar，OCI tar 默认采用同目录下的 `<输出文件名去掉 .tar>.oci.tar`，也可用 `--oci-output` 指定。

```bash
python3 main.py --dockerfile /data/project/Dockerfile --context /data/project \
  --base-map /data/images/base-map.json --tag mycompany/app:1 \
  --output /data/images/app.tar --format both \
  --oci-output /data/images/app.oci.tar --run
```

OCI tar 根目录包含 `oci-layout`、`index.json` 和 `blobs/sha256/*`；index 引用单架构 manifest，manifest 引用 config 与按顺序排列的**未压缩** layer tar。每个 descriptor 都有 media type、SHA-256 digest 和字节长度；`org.opencontainers.image.ref.name` 保留完整构建标签。OCI Layer tar 的 digest 与 config 的 diff ID 相同。两种输出复用同一份构建 config 和层内容，生成后分别验证布局、引用、长度和哈希。

Docker tar 面向目标机的 `docker load`；OCI tar 面向支持 OCI layout archive 的导入流程。OCI 布局本身不是 Registry 上传协议，上传由独立的 `registry.py` 完成。不同版本的目标导入程序可能采用不同的标签解析规则，需在目标环境验证；本机测试覆盖归档内部结构和双格式内容一致性，尚未在 Docker/Podman/containerd 上实测导入。

## Phase 8：可重复构建

默认使用 `SOURCE_DATE_EPOCH=0`（1970-01-01 UTC）。可设置同名环境变量，或用 `--source-date-epoch` 显式指定非负 Unix 秒数；显式参数优先。Windows `--wsl` 会把已解析的值传给 WSL 构建进程。

```bash
python3 main.py --dockerfile /data/project/Dockerfile --context /data/project \
  --base-tar /data/images/base-image.tar --tag mycompany/app:1 \
  --output /data/images/app.tar --format both \
  --source-date-epoch 1700000000
```

构建器用该值写新 layer 的 mtime、image config 的 `created`/history 时间、Docker 与 OCI 外层 tar 成员时间；外层 tar 不再带入临时文件的宿主 UID/GID、权限或修改时间。它也作为 `SOURCE_DATE_EPOCH` 环境变量传给 `RUN`（Dockerfile `ENV` 明确设置同名变量时以其值为准），并进入缓存键。已有基础镜像层保持原始字节。相同的基础归档、Dockerfile、context 文件内容与相关元数据、构建参数及确定性的 `RUN` 行为，会得到逐字节相同的 Docker/OCI tar 和 SHA-256；缓存命中与否不影响结果。修改来源文件的宿主 mtime 而不改变其内容或其他元数据，也不会改变新层字节。

**边界：**任意 `RUN` 脚本仍可能把当前时间、随机值、网络响应、路径或并发顺序写入文件内容；仅固定 tar 的 mtime 无法消除这些差异。内网依赖仓库和工具链版本也应固定。`RUN` 使用的基础镜像与构建机的 Linux 执行环境仍需目标机实测；本机测试覆盖确定性归档、缓存路径、mtime 归一化及模拟执行器，不构成对任意 Dockerfile 的可重复性保证。

**运行权限与阶段边界：**默认 `hardened` 执行器要求 Linux root 及相应内核能力；`rootless` 模式还受后述单 UID/GID 限制。没有满足条件时会报错。沙箱降低部分宿主机风险，但不能作为运行不可信 Dockerfile 的完整安全边界。详细机制见 [ARCHITECTURE.md](ARCHITECTURE.md)。

输出文件已存在时拒绝覆盖。执行 `RUN` 时建议 Linux 临时文件系统有至少基础镜像解包大小约 2～3 倍的空间。本机 Windows 测试覆盖合成镜像格式与哈希、执行器编排、whiteout 转换、`ADD` 解包、所有权/权限/链接和失败中止；**未实测 Linux mount/chroot/实际 RUN**。请在目标 Linux/WSL 环境先运行随包的真实执行冒烟测试（基础镜像须含 `/bin/sh` 与 `rm`）：

```bash
python3 tests/linux_run_smoke.py --base-tar /data/images/base-image.tar \
  --from mycompany/base:1 --workspace /tmp
```

它验证 `COPY` 数字所有权与权限、本地 `ADD` 解包、`RUN` 创建文件、删除/whiteout 和 `RUN false` 中止。随后再在另一台有 Docker Engine 的机器执行 `docker load -i output-image.tar` 与应用启动验证。

## Phase 9：离线 SBOM、provenance 与签名

不需要 Docker、Podman、网络或第三方 Python 包。`--attest` 为每次构建额外生成 SPDX 2.3 JSON 文件清单和 SLSA provenance v1 的 in-toto Statement。`--sign-key` 还会用 Ed25519 对 provenance 制作 DSSE 封套，并自动启用 `--attest`。在打包机上创建私钥和公钥：

```bash
python3 attest.py keygen --private-key /secure/build-key.hex \
  --public-key /secure/build-key.pub.hex
```

将**公钥**预先通过可信渠道交给验收方；私钥只留在受控打包机，不放入镜像、交付目录或源码。生成一套双格式交付件：

```bash
python3 main.py --dockerfile /data/project/Dockerfile --context /data/project \
  --base-map /data/images/base-map.json --tag mycompany/app:1 \
  --output /data/delivery/app.tar --format both --sign-key /secure/build-key.hex
```

产物为 `app.tar`、`app.oci.tar`、`app.tar.spdx.json`、`app.tar.provenance.json`、`app.tar.dsse.json`。`--attest` 不带 `--sign-key` 时只生成前两种旁路文件，不生成签名。`--format docker` 或 `oci` 时只生成选定归档。发布前检查所有目标文件，已有文件拒绝覆盖；归档和旁路文件在临时工作区完成后才发布，发布失败时清理本次已发布的文件。

在验收机上，仅用 Python 和预先受信任的公钥验签：

```bash
python3 attest.py verify --public-key /secure/build-key.pub.hex \
  --provenance /data/delivery/app.tar.provenance.json \
  --envelope /data/delivery/app.tar.dsse.json \
  --sbom /data/delivery/app.tar.spdx.json \
  --docker-archive /data/delivery/app.tar \
  --oci-archive /data/delivery/app.oci.tar
```

校验包括：DSSE 载荷与 provenance 文件逐字节一致、Ed25519 签名与**指定公钥**匹配、provenance 中每份镜像归档的 SHA-256 与交付文件一致、SBOM 的 SHA-256 与 provenance 一致。双格式构建必须同时提供两份归档。签名不证明构建机可信或 Dockerfile 安全；公钥可信分发与私钥保护是验收前提。

SBOM 枚举最终可见 rootfs 的普通文件和硬链接，给出 SHA-1/SHA-256，并检测 dpkg、APK、Python dist-info 以及 WAR/JAR/EAR 中的 Maven `pom.properties`；嵌套 JAR 扫描一层，单个归档上限 128 MiB，嵌套 JAR 上限 64 MiB。未识别的依赖、压缩包内部普通文件、符号链接、特殊文件、许可证结论与漏洞状态不会自动推断。特别是任意 `RUN` 下载内容或使用的外部网络服务，不会因 provenance 而变得可追溯；`resolvedDependencies` 记录 Dockerfile、构建上下文内容清单及实际使用的本地基础归档摘要。

签名实现是随包提供的纯 Python Ed25519，已用 RFC 8032 测试向量及篡改测试验证，但**没有独立安全审计，且 Python 大整数运算并非常数时间**。它适合受控内网打包、离线验收的基础交付链；对高价值生产签名密钥，应在组织安全评估后采用经过审计的签名实现和密钥保护方案。固定 `SOURCE_DATE_EPOCH` 且构建行为确定时，SBOM、provenance、签名封套以及镜像归档均可逐字节复现；交付文件名不会写入 provenance。

## Phase 10：Rootless 与 RUN 沙箱强化

`--run-sandbox` 有三个值：`hardened`（命令行和 Python API 默认）、`rootless`、`legacy`。三者都创建 mount/PID namespace，并在默认 `--run-network none` 时创建 network namespace。`hardened` 和 `rootless` 在镜像内执行 `RUN` 前设置 `no_new_privs`、清除进程及 bounding-set capabilities，并安装 seccomp 过滤器，拒绝挂载、切换 namespace、ptrace、BPF、内核模块和部分高风险系统调用。过滤器只支持 Linux x86_64、aarch64；不支持时失败。`legacy` 保留 Phase 2 行为，供需要额外权限的可信构建脚本显式选择。

`rootless` **必须由非 root Linux 用户启动**，将该用户的 UID/GID 一对一映射为用户命名空间中的 0:0；不会提权到宿主机 root，也不使用 `newuidmap`、`newgidmap` 或其他外部构建工具。由于仅映射一个 UID/GID，基础镜像及新增层中的文件必须属于 0:0，`RUN` 的 `USER` 必须解析为 0:0，镜像中的设备节点及不能保留的 xattr 会失败。很多真实基础镜像带非零所有权，需先检查或使用 `hardened`。rootless OverlayFS 需要内核和承载 `/tmp`/工作区的文件系统支持用户命名空间挂载及 `userxattr`；被发行版策略或 WSL 禁用时会明确失败，不自动降级为 root。

```bash
# 在非 root Linux 用户下运行；基础镜像须满足单 UID/GID 限制
python3 main.py --dockerfile /data/project/Dockerfile --context /data/project \
  --base-tar /data/images/base.tar --tag mycompany/app:1 \
  --output /data/output/app.tar --run --run-sandbox rootless \
  --workspace /tmp

# 在 Windows 上使用 WSL 中的非 root 用户
python main.py --dockerfile C:\project\Dockerfile --context C:\project \
  --base-tar C:\images\base.tar --tag mycompany/app:1 \
  --output C:\output\app.tar --run --wsl --run-sandbox rootless \
  --wsl-user builder
```

`--run-network host` 明确允许宿主网络；默认 `none` 应保持。沙箱未实现 cgroup 资源配额、完整系统调用白名单、文件系统外部依赖审计或供应链隔离；用户命名空间本身也增加可访问的内核代码路径。`RUN` 命令仍可能耗尽 CPU、内存或磁盘，并可利用未知内核漏洞。内网中执行未经审查的 Dockerfile，仍应放在单独的、权限受控的构建机或虚拟机。

目标 Linux 上执行真实冒烟测试：

```bash
python3 tests/linux_run_smoke.py --base-tar /data/images/base.tar \
  --from mycompany/base:1 --sandbox hardened
# 若镜像所有文件均为 0:0，另在非 root 用户下测试：
python3 tests/linux_run_smoke.py --base-tar /data/images/base.tar \
  --from mycompany/base:1 --sandbox rootless
```

本机 Windows 测试仅覆盖模式传递、UID 限制、seccomp 过滤器结构和缓存隔离；当前没有 WSL 发行版，尚未实测 Linux namespace、mount、seccomp 和 rootless `RUN`。部署前必须在目标内核及文件系统上运行上述测试。

## Phase 11：内网 Harbor / OCI Registry push、pull

`registry.py` 是独立的纯 Python Registry v2 客户端。基础镜像可拉成 Docker save tar，直接交给 `main.py --base-tar`；构建产物可从 Docker save tar 或本工具生成的 OCI tar 推送。推送会把未压缩 layer 确定性地转为 gzip layer，保持 image config 中的 diff ID；远端 manifest digest 因此与本地 OCI tar 内的 manifest digest 不同。拉取时先校验 manifest/blob SHA-256 和大小，再校验解压后 layer diff ID，并生成已验证的本地归档。

```bash
export PYIMAGEBUILDER_REGISTRY_PASSWORD='从受控渠道取得的 Harbor robot 密码'

python3 registry.py --username 'robot$builder' --ca-file /data/harbor-ca.pem \
  pull harbor.intra:8443/project/base:1 --output /data/images/base.tar

python3 main.py --dockerfile /data/project/Dockerfile --context /data/project \
  --base-tar /data/images/base.tar --tag harbor.intra:8443/project/app:2 \
  --output /data/images/app.tar --format both

python3 registry.py --username 'robot$builder' --ca-file /data/harbor-ca.pem \
  push harbor.intra:8443/project/app:2 --archive /data/images/app.oci.tar
```

Windows PowerShell 可用 `$env:PYIMAGEBUILDER_REGISTRY_PASSWORD = '...'` 设置密码，然后执行 `python .\registry.py ...`。`--username` 与密码环境变量必须同时提供；匿名拉取可同时省略。不会从命令行参数读取密码，也不会把密码写入镜像归档。`--ca-file` 用于验证内网 HTTPS 私有 CA。仅在受控的明文测试 Registry 上显式使用 `--insecure-http`；它会让凭据通过 HTTP 传输。若 Bearer token 服务使用不同主机名，需明确加 `--auth-host 主机名[:端口]` 信任该地址。

`pull --format docker|oci|both` 可选；默认 Docker tar，`both` 时另产生 `<output-stem>.oci.tar`，也可用 `--oci-output` 指定。支持 OCI/Docker schema 2 的单架构 manifest，以及从 image index/manifest list 精确选择一个 `linux/amd64` 或 `linux/arm64` 子镜像（`--platform`）。按 digest 拉取时，生成的本地归档会使用 `:pulled-<digest前12位>` 标签。Phase 12 增加双平台 index 推送；当前仍不支持 schema 1、foreign layer、断点续传、Registry referrers/签名上传；Phase 9 的 SBOM 与签名仍作为本地交付文件保存。推送前请在 Harbor 目标项目中赋予 robot 帐号 pull/push 权限。目标 Harbor 与其证书、权限策略仍需在内网实测。

标准 Registry / Harbor 拉取会并发下载及解压校验各层，并在写出时保持 manifest 中的原始层顺序。`main.py pull --workers N`、`main.py build --pull-workers N` 可限制并发数；默认 `0` 表示最多 4 个下载线程。独立的 `registry.py pull --workers N` 也可使用。Artifactory 目录下载沿用自己的自适应并发策略。

推送默认拒绝覆盖已有 tag；确实需要更新该 tag 时，在 `push` 子命令末尾显式加 `--overwrite`。上传 blob 后若 manifest PUT 失败，Registry 可能留下尚未被引用的 blob，客户端不会删除远端数据。

协议参考：[OCI Distribution Specification](https://github.com/opencontainers/distribution-spec/blob/main/spec.md)、[Docker Registry Bearer 认证](https://docs.docker.com/reference/api/registry/auth/)、[OCI media types](https://github.com/opencontainers/image-spec/blob/main/media-types.md)。

## Phase 12：amd64 / arm64 跨架构构建

`--platform linux/amd64|linux/arm64` 指定默认目标平台，默认仍为 `linux/amd64`。Dockerfile 中的字面量 `FROM --platform=linux/arm64 image AS stage` 可覆盖单个阶段；变量形式暂不支持。`FROM scratch`、本地基础镜像选择、最终 config、OCI descriptor 和 Registry 拉取都会按平台校验。无 `RUN` 时，两种架构均只需 Python 标准库，无需 Docker、Podman、QEMU 或 WSL。工具不会把 amd64 的 WAR/JAR、可执行文件或本地二进制转换成 arm64 版本。

同一基础镜像引用若对应两种架构，可在 `--base-map` JSON 中按平台配置：

```json
{
  "harbor.intra/project/base:1": {
    "linux/amd64": "/data/images/base-amd64.tar",
    "linux/arm64": "/data/images/base-arm64.tar"
  }
}
```

分别构建单平台 OCI tar，再合并并推送一个双平台 tag：

```bash
python3 main.py --dockerfile /data/project/Dockerfile --context /data/project \
  --base-map /data/images/base-map.json --platform linux/amd64 \
  --tag harbor.intra/project/app:1 --format oci --output /data/images/app-amd64.oci.tar
python3 main.py --dockerfile /data/project/Dockerfile --context /data/project \
  --base-map /data/images/base-map.json --platform linux/arm64 \
  --tag harbor.intra/project/app:1 --format oci --output /data/images/app-arm64.oci.tar
python3 multiarch.py --amd64 /data/images/app-amd64.oci.tar \
  --arm64 /data/images/app-arm64.oci.tar --output /data/images/app-multi.oci.tar \
  --tag harbor.intra/project/app:1
python3 registry.py --username 'robot$builder' --ca-file /data/harbor-ca.pem \
  push-index harbor.intra/project/app:1 --archive /data/images/app-multi.oci.tar
python3 registry.py --username 'robot$builder' --ca-file /data/harbor-ca.pem \
  pull harbor.intra/project/app:1 --platform linux/arm64 \
  --output /data/images/app-arm64-pulled.tar
```

`push-index` 和单平台 `push` 一样，默认拒绝覆盖已有 tag；更新时显式加 `--overwrite`。合并结果是**多平台 OCI layout tar**，用于 Registry 发布或支持 OCI index 的导入程序；给 `docker load` 时应分别交付单平台 Docker save tar。合并步骤不会自动给多平台 index 生成 Phase 9 签名；已有签名仍仅约束各自的单平台交付件。

跨架构 `RUN` 必须在 Linux/WSL 上显式使用 `--run --allow-emulated-run`，且宿主机已配置并启用对应架构的 binfmt_misc/QEMU **F 标志**解释器，构建器才会尝试执行。还须满足现有 namespace、OverlayFS 和沙箱权限条件。只有 Python、没有可用 QEMU/binfmt 的内网机可打包外架构镜像，但无法执行外架构 `RUN`；工具不会安装或修改宿主机解释器注册。缓存命中可能绕过实际执行，首次执行及缓存失效时仍需上述环境。当前 Windows 测试已验证平台选择、归档摘要及模拟 Registry 往返，尚未在真实 arm64 主机、QEMU/WSL 或 Harbor 上实测跨架构 `RUN` 和导入。

格式依据：[OCI image config](https://github.com/opencontainers/image-spec/blob/main/config.md)、[OCI image index](https://github.com/opencontainers/image-spec/blob/main/spec.md)；F 标志见 [Linux binfmt_misc 文档](https://docs.kernel.org/admin-guide/binfmt-misc.html)。

## Phase 13：ARG 与 RUN cache/secret mount

支持 `ARG NAME[=default]`、重复 `--build-arg NAME=value`，以及 `FROM` 前的全局 ARG。全局 ARG 可供 `FROM` 和 `FROM --platform` 使用；阶段内须重新声明后才进入该阶段的变量环境。内置 `TARGETPLATFORM`、`TARGETARCH`，可识别宿主架构时另提供 `BUILDPLATFORM`、`BUILDARCH`。当前支持 `$NAME` 与 `${NAME}` 在 `FROM` 镜像名、`FROM --platform`、`ENV` 值、`LABEL` 值、`WORKDIR`、`COPY/ADD` 源和目标中展开；`RUN` shell 命令由镜像内 shell 在执行时读取 ARG 环境。复杂 `${VAR:-default}` 表达式和其他 Dockerfile 指令中的替换暂不支持。ARG 不写入最终镜像 `Env`，但它可能通过 `RUN` 输出或 Dockerfile 默认值进入镜像记录；**不要把密码放入 ARG**。

```dockerfile
ARG BASE=harbor.intra/project/build-base:1
FROM ${BASE} AS build
ARG VERSION=1.0
ARG TARGETPLATFORM
RUN --mount=type=cache,target=/root/.cache/pip,id=pip-cache \
    --mount=type=secret,id=pip_token \
    python3 -m pip install -r requirements.txt
```

```bash
python3 main.py --dockerfile /data/project/Dockerfile --context /data/project \
  --base-map /data/images/base-map.json --build-arg VERSION=1.1 \
  --secret pip_token=/secure/pip_token.txt --run \
  --cache-dir /data/pyimagebuilder-cache \
  --tag harbor.intra/project/app:1 --output /data/images/app.tar
```

`RUN --mount=type=cache` 支持 `target`、可选 `id`、`sharing=locked`；同一平台与 id 的目录保存在 `--cache-dir/run-mounts`，供以后构建复用，且同一 id 的并发写入会加锁。`--no-cache` 时 mount 只在本次临时工作区有效。`RUN --mount=type=secret` 支持 `id`、可选绝对 `target`（默认 `/run/secrets/<id>`）和 `required=true|false`；源文件只在该条 RUN 的 mount namespace 中只读绑定，mount 路径从生成的 layer 排除。带 secret 的 RUN 每次都实际执行，不使用指令层缓存。若命令主动把 secret 复制到其他路径或打印到日志，工具无法阻止泄露；应审查 Dockerfile 和构建日志。secret 文件应位于受控目录，不能放进 build context，避免 `COPY .` 收录。

目前仅实现上述 cache/secret 选项；`type=bind/tmpfs/ssh`、cache 的 `sharing=shared/private`、secret 的环境变量形式、`RUN --network` 指令级选项及 heredoc 暂不支持，遇到会报错。secret 文件按宿主文件权限只读挂载，镜像内非 root `USER` 也必须有相应读取权限。cache/secret mount 仍需要现有 Linux/WSL `RUN` 环境；只有 Python 的机器可解析这些 Dockerfile，但无法执行含 `RUN` 的构建。本机自动测试覆盖解析、缓存与挂载参数传递，尚未在目标 Linux/WSL 上实测真实 bind mount。请在目标机使用含 `/bin/sh` 的基础镜像运行：

```bash
python3 tests/linux_phase13_smoke.py --base-tar /data/images/base.tar \
  --from harbor.intra/project/base:1 --workspace /tmp
```

语义参考：[Dockerfile ARG](https://docs.docker.com/reference/dockerfile/#arg)、[RUN cache mount](https://docs.docker.com/reference/dockerfile/#run---mounttypecache)、[RUN secret mount](https://docs.docker.com/reference/dockerfile/#run---mounttypesecret)。

## Phase 14：镜像分析与保守优化

`optimizer.py` 离线读取并验证单平台 Docker save tar 或本工具生成的未压缩层 OCI tar，输出 JSON 报告。报告列出层 tar 大小、文件有效负载、被后续层覆盖或删除的文件字节、各层最大的文件，以及镜像各层中 SHA-256 相同的文件内容。重复内容统计包含隐藏的旧文件，因此是**排查线索**，不是可以直接节省的字节数。

```bash
python3 optimizer.py analyze --archive /data/images/app.tar \
  --large-mib 50 --output /data/reports/app-analysis.json

python3 optimizer.py optimize --archive /data/images/app.tar \
  --output /data/images/app-pruned.tar
```

`optimize` 逐层试删，仅在最终可见 rootfs 的路径、类型、文件 SHA-256、权限、所有权、链接和 tar 元数据完全一致时才移除该层；随后更新 config 的 diff IDs/history，写出与输入相同格式的新归档并重新验证。含硬链接或特殊文件的镜像会拒绝自动删层，避免误判。不能独立移除的层保持原样；即使报告有大量隐藏字节，也可能没有任何完整层可删。输出归档必须是新路径，原镜像不修改。可用于 OCI 单平台归档；双平台 OCI index 请先分别分析各平台镜像。优化会改变镜像 digest，旧 SBOM/provenance/签名不再对应新文件，需重新生成和签署。最终运行效果仍应在目标 Docker/Podman 环境验证。

层缓存可单独审计；默认只报告版本过旧、元数据损坏、层缺失或摘要不符的条目，以及无有效条目引用的孤立层：

```bash
python3 optimizer.py cache --cache-dir /data/pyimagebuilder-cache
python3 optimizer.py cache --cache-dir /data/pyimagebuilder-cache --prune
```

`--prune` 才会删除报告中的失效条目和孤立层，不清理 Phase 13 的 RUN cache mount 数据。清理时应暂停该缓存目录上的并发构建。仅凭磁盘条目无法推断缓存命中率或外部依赖是否过期；分析报告也不会自动改写 Dockerfile。

## Phase 15：Conformance 兼容性测试

`conformance.py` 是独立的验收入口。`offline` 不需要 Docker、Podman 或网络；它从固定 Dockerfile 样例构建 Docker/OCI tar，再用**独立于构建器的归档读取和层应用代码**检查文件内容、权限、所有权、配置、whiteout、链接、多阶段隔离和缓存失效。当前有 7 个场景。报告中的 `docker_reference_executed=false` 表示这一步没有与 Docker 实际对照。

```bash
python3 conformance.py offline --report /data/reports/conformance-offline.json
```

在有 Linux/WSL 的打包机上，还可用含 `/bin/sh` 与 `rm` 的基础镜像实际运行 namespace、OverlayFS、RUN、whiteout、cache/secret mount 冒烟测试：

```bash
python3 conformance.py linux --base-tar /data/images/base.tar \
  --from harbor.intra/project/base:1 --workspace /tmp \
  --report /data/reports/conformance-linux.json
```

在**另有 Docker Engine 的验收机**上运行对照模式。它对相同的内置样例执行 `docker build`，比较两边 `docker save` 的最终配置与 rootfs，再对本工具产物执行 `docker load`、`docker inspect`、`docker create`、`docker export`，对比 Docker 实际物化的文件树。测试使用唯一临时 tag/container 名，结束后删除这些测试资源；请在专用验收机上运行。

```bash
python3 conformance.py reference --report /data/reports/conformance-docker.json
```

对**自己的 Dockerfile 和已构建 tar**做 Docker 构建结果对比：

```bash
python3 conformance.py compare --archive /data/images/app.tar \
  --tag harbor.intra/project/app:1 --dockerfile /data/project/Dockerfile \
  --context /data/project --report /data/reports/app-diff.json
```

`compare` 需要验收机 Docker daemon 能获取或已预加载 Dockerfile 的基础镜像；默认使用 `--pull=false --network=none`，但缺失的基础镜像仍可能使 Docker 尝试访问 Registry，内网环境应先预加载。若原构建使用了 ARG 或 secret，可重复传入 `--build-arg NAME=value`、`--secret ID=/file`；确需联网的对照 `RUN` 可显式用 `--docker-network host`。若 Dockerfile 有 `RUN`，对照构建会在 Docker 环境中执行它。此命令比较 Docker 构建结果，不自动 `docker load` 用户提供的 tar，以免覆盖用户已有 tag；内置 `reference` 样例才执行 load/inspect/export 测试。

各命令均输出 JSON，所有检查通过退出码为 0；发现差异为 1；输入或工具环境错误为 2。默认忽略 tar 修改时间和部分隐式父目录表示差异，重点比较最终路径、文件摘要、权限/属主、链接、可表示的 xattr 及运行配置。没有 Docker 的机器只能得出**离线格式与固定样例通过**的结论，不能据此宣称与 Docker 全面兼容；真实 `RUN`、目标 daemon 版本、应用启动和网络行为仍需在相应环境验证。格式与语义依据：[OCI layer](https://github.com/opencontainers/image-spec/blob/main/layer.md)、[OCI image config](https://github.com/opencontainers/image-spec/blob/main/config.md)、[Dockerfile reference](https://docs.docker.com/reference/dockerfile/)。

## Phase 16：严格封闭构建

`hermetic.py` 先为本地输入生成 JSON 锁文件；`main.py --hermetic` 校验锁文件，将 Dockerfile、整个 context 和基础镜像 tar 复制到临时快照，从快照构建，发布前再次校验原输入，并生成 `<输出.tar>.hermetic.json`。锁中记录文件路径、类型、权限、大小与 SHA-256、基础归档摘要、构建参数摘要，以及 Python、操作系统架构和构建器源码指纹。构建固定时间戳，不读写层缓存。相同锁、工具链和输入可得到相同归档字节。

在 **PowerShell** 中，先在启动 Python 前设置固定哈希种子：

```powershell
$env:PYTHONHASHSEED = '0'
python hermetic.py --dockerfile C:\project\Dockerfile --context C:\project `
  --base-tar C:\images\base.tar --tag example/app:1 `
  --source-date-epoch 0 --output C:\images\app.inputs.lock.json
python main.py --dockerfile C:\project\Dockerfile --context C:\project `
  --base-tar C:\images\base.tar --tag example/app:1 `
  --source-date-epoch 0 --output C:\images\app.tar `
  --hermetic --input-lock C:\images\app.inputs.lock.json
```

Linux 上使用 `export PYTHONHASHSEED=0` 后执行同样两步。`FROM scratch` 无须 `--base-tar`。多基础镜像使用 `--base-map`；多阶段、`--target`、`--format docker|oci|both`、`--platform` 和 `--build-arg` 必须在锁定与构建两步保持一致。锁文件与输出必须放在 context 外。输出文件已存在时会拒绝覆盖。可用 `--format both` 同时生成 Docker 与 OCI tar。

此模式**拒绝所有 `RUN`**，也拒绝 context 符号链接/硬链接、宿主架构内置变量 `BUILDPLATFORM`/`BUILDARCH`、WSL、secret、持久层缓存、签名/attestation、模拟执行与自定义工作区。任意 `RUN` 能观察时钟、随机数、内核、网络、宿主 CPU 等状态，仅靠 Python 加 namespace 无法保证严格封闭；如需要 `RUN`，请使用普通构建模式，并在能控制执行环境的 Linux 主机上另行验证。`--hermetic` 提供的是**可信 Python 和宿主文件系统下，对声明的本地文件输入的封闭与一致性校验**；它不是抵抗恶意并发进程或被攻陷内核的安全边界，也不证明最终程序运行结果与 Docker 完全一致。
