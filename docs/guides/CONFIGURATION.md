# 统一配置

[文档目录](../README.md) · [项目首页](../../README.md) · [配置模板](../../config.example.json)

配置使用 **JSON，schemaVersion 为 2**。仓库连接、缓存路径和 Fast 模板集中到同一份 `config.json`，不再维护独立 fast.json。普通 build 可以无配置运行；Fast 需要有效 profile。

## 1. 找到正在使用的配置

读取顺序如下，找到后停止：

1. 命令行显式 `--config 路径`。
2. 环境变量 `PYIMAGEBUILDER_CONFIG`。
3. 当前工作目录的 `config.json`。
4. 工具脚本目录的 `config.json`。

显式指定的文件不存在会报错。**配置里的相对路径以配置文件所在目录为基准**；命令行路径通常以执行命令的当前目录为基准。

需要区分多套环境时，建议每次传同一份 `--config`：

```powershell
python .\main.py pull harbor.internal/project/base:1 --config D:\builder-config\config.json
python .\main.py build D:\project -t company/app:1 -o D:\images\app.tar `
  --config D:\builder-config\config.json
```

## 2. 创建或修改配置

配置文件尚不存在时，可以复制 `config.example.json` 为 `config.json`，替换其中的示例地址和部署设置。文件已经存在时直接编辑，避免覆盖自己的配置。也可以用 [fast init](FAST.md#1-首次配置基础镜像) 创建新 profile。init 的写入位置取显式 `--config`、环境变量指定的路径，或者当前目录的 config.json；从其他目录创建模板时建议显式传 `--config`。

| 顶层字段 | 管理什么 |
| --- | --- |
| `schemaVersion` | 配置版本，当前为 2 |
| `cache` | 共享镜像库和指令层缓存的位置 |
| `repositories` | 按仓库主机名设置下载方式、账号、密码环境变量与 CA |
| `fast` | 默认 profile 与多个 TongWeb/Tomcat 打包模板 |
| `buildRecordsDir` | 可选的成功打包台账目录；Web 任务摘要也存放在该目录下 |

使用 UTF-8 JSON，可带 Windows 编辑器生成的 BOM。配置不允许注释、尾随逗号、重复键或 NaN/Infinity；`schemaVersion` 必须是整数 `2`，不能写成 `2.0` 或字符串。顶层、cache、repositories 和 fast 各层只接受已定义字段，拼错字段或放错层级会立即报错。

| 配置层级 | 支持的字段 |
| --- | --- |
| `cache` | `imageStore`、`layerCache` |
| `repositories.<host>` | `source`、`username`、`passwordEnv`、`caFile`、`insecureHttp`、`verifyTls`、`workers`、`authHost` |
| `fast` | `defaultProfile`、`profiles` |
| `fast.profiles.<name>` | `flavor`、`baseImage`、`baseTar`、`baseUrl`、`owner`、`deployDir`、`serverConfig`、`outputDir` |

`defaultProfile` 若设置，必须指向已有 profile；所有 profile 都先做字段及文本类型检查，选中使用时再核对镜像来源、部署路径和文件存在性。鉴权字段放在 `repositories`，下载位置放在 `cache.imageStore`；profile 中的旧 `baseCacheDir` 不再接受。`serverConfig`、`outputDir` 可省略或设为 null。

## 3. 下载地址与鉴权

```json
{
  "schemaVersion": 2,
  "cache": {
    "imageStore": "D:/builder-data/image-store",
    "layerCache": "D:/builder-data/layers"
  },
  "repositories": {
    "nmkf.zpk.abc": {
      "source": "artifactory",
      "username": "robot-account",
      "passwordEnv": "PYIMAGEBUILDER_ARTIFACTORY_PASSWORD"
    },
    "harbor.internal": {
      "source": "registry",
      "username": "robot-account",
      "passwordEnv": "PYIMAGEBUILDER_REGISTRY_PASSWORD",
      "caFile": "./certs/internal-ca.pem"
    }
  },
  "fast": {}
}
```

仓库主机名、账号和 CA 文件均需替换。`source=registry` 用于标准 Registry/Harbor；`source=artifactory` 用于本项目支持的镜像文件目录下载。`caFile` 是可选字段，配置后文件必须存在。

如果服务的实际地址是 `http://`，在对应的 `repositories.<host>` 中设置 `"insecureHttp": true`；
如果实际是 `https://`，设为 false，使用默认系统信任或配置企业 CA 的 `caFile`。
`insecureHttp` 表示切换到 HTTP，不是跳过 HTTPS 证书校验。

| 连接方式 | `insecureHttp` | `verifyTls` | CA |
| --- | --- | --- | --- |
| HTTP | true | 不适用 | 不使用 |
| HTTPS，正常校验证书（默认） | false | true 或省略 | 系统信任，或 `caFile` |
| HTTPS，兼容无效证书 | false | false | 对该仓库不校验证书 |

`verifyTls` 只接受 JSON 布尔值，不接受字符串 `"false"`。兼容模式仍使用 HTTPS 加密，
但不验证对方身份；仅在明确需要兼容时启用，优先修复证书或配置企业 CA。
例如浏览器已手动接受 nmkf 的无效证书，可在**现有仓库项**中设置：

```json
"nmkf.zpk.abc": {
  "source": "artifactory",
  "username": "实际账号",
  "passwordEnv": "PYIMAGEBUILDER_ARTIFACTORY_PASSWORD",
  "insecureHttp": false,
  "verifyTls": false
}
```

这个例外只适用于该仓库来源（协议、主机、端口），不会关闭其他仓库、跨来源 blob 下载或独立 token 服务的证书校验。
浏览器的证书例外不会传给 Python；程序也不会在证书失败后自动降级到 HTTP。

Fast、普通 build/pull、Web 模板测试与仓库诊断使用同一仓库策略。
显式设置 `insecureHttp` 时，它优先于 Fast `baseUrl` 中的协议，地址路径和镜像引用保持不变。
Artifactory 仓库未设置该字段时，Fast 使用选中模板的 URL；按镜像引用拉取和诊断从同仓库模板的唯一协议推断。
若同仓库模板同时使用 HTTP 和 HTTPS，则这些无 URL 的操作会要求明确设置 `insecureHttp`；没有模板可推断时默认 HTTPS。
独立 `artifactory_download.py` 的命令行 URL 保留显式协议；支持 `--insecure-http` 的客户端仍允许命令行切换到 HTTP。
示例配置中的 nmkf 演示 HTTP 服务，其他 HTTPS 仓库默认严格校验证书。

修改 UOS 上的 `config.json` 后重启 Web 工作台。仓库诊断下拉项会显示 HTTP/HTTPS 和证书兼容状态，
任务日志会显示当前实际检查的地址；浏览器登录页能打开不代表同一地址上的 HTTPS 证书已经受 Python 信任。

在运行命令的终端设置配置指定的密码变量。以 UOS/Bash 的 Artifactory 为例：

```bash
read -r -s -p '仓库密码: ' PYIMAGEBUILDER_ARTIFACTORY_PASSWORD
echo
export PYIMAGEBUILDER_ARTIFACTORY_PASSWORD
```

Registry 配置若使用 `PYIMAGEBUILDER_REGISTRY_PASSWORD`，将变量名换成对应名称。`read` 隐藏输入，`export` 将变量传给后续 Python 进程；PowerShell 的隐藏输入示例见 [Web 指南](WEB.md#1-启动与仓库密码)。

启动 Web 前也要设置变量；网页没有仓库密码输入框，已启动进程不会读取另一个终端后设置的变量。

`passwordEnv` 存的是变量名，程序运行时读取其值。JSON 中不允许 `password` 或 `token` 字段。连接参数通常可用相应命令的 `--username`、`--password-env`、`--ca-file` 覆盖；Fast 日常命令使用 profile 对应的 repositories 设置。

Artifactory 地址也不能写成 `https://用户名:密码@主机/...`：这类地址会在解析或请求前被拒绝，防止凭据进入来源记录。地址中请勿携带其他敏感查询参数；程序不提供通用的 URL 或 RUN 日志脱敏。

需要限制下载并发时，仓库项可设置 `workers`（0–64，0 表示自动选择）。其他连接字段请结合当前子命令的 `--help` 使用。

## 4. Fast 模板字段

将下面的 `fast` 对象放到完整配置内，而不是另建文件：

```json
{
  "defaultProfile": "tongweb",
  "profiles": {
    "tongweb": {
      "flavor": "tongweb",
      "baseImage": "nmkf.zpk.abc/repo/tongweb:11",
      "baseUrl": "https://nmkf.zpk.abc/repo/tongweb:11",
      "owner": "tongadmin:tonggrp",
      "deployDir": "/soft/TongWeb7.0/autodeploy",
      "serverConfig": "./config/tongweb.xml",
      "outputDir": "D:/images"
    },
    "tomcat": {
      "flavor": "tomcat",
      "baseImage": "company/tomcat:9",
      "baseTar": "D:/images/tomcat-base.tar",
      "owner": "tomcat:tomcat",
      "deployDir": "/soft/tomcat/webapps",
      "outputDir": "D:/images"
    }
  }
}
```

| 字段 | 如何理解 |
| --- | --- |
| `defaultProfile` | 没有传 `--profile` 时的模板；添加新 profile 不会自动替换已有默认项 |
| `profiles` 下的名称 | 模板选择名，如 tongweb、tomcat；不是镜像版本号 |
| `flavor` | 使用 tongweb 或 tomcat 的部署规则 |
| `baseImage` | 临时 Dockerfile 的 FROM 引用，必须与实际基础输入一致 |
| `baseUrl` / `baseTar` | **二选一**：Artifactory 下载地址，或本地 Docker save tar |
| `owner` | COPY 写入镜像时的 Linux 属主，与宿主 Windows 用户无关 |
| `deployDir` | 镜像内应用部署目录，须符合基础镜像 |
| `serverConfig` | 可选的本地 TongWeb XML；Tomcat profile 不支持此字段 |
| `outputDir` | 最终镜像输出目录，可由 Fast 的 `-o` 覆盖 |

`baseUrl` 解析出的引用必须与 `baseImage` 一致。模板不能靠把 profile 命名为 tomcat 就自动适配任意 Tomcat 基础镜像；部署路径和账户仍需核对。

## 5. 缓存与运行数据

| 数据 | 配置字段 | 命令行覆盖 | 用途 |
| --- | --- | --- | --- |
| 基础镜像库 | `cache.imageStore` | `--image-store`（支持此参数的命令） | refs、CAS blob、按需 Docker tar |
| 指令层缓存 | `cache.layerCache` | `--cache-dir` | 复用 COPY/ADD/RUN 等构建结果 |
| Fast 输出 | profile 的 `outputDir` | `-o` | 最终交付镜像 |
| 打包台账 | `buildRecordsDir`（顶层，可选） | 环境变量 `PYIMAGEBUILDER_BUILD_RECORDS_DIR` | 每次成功打包的一条记录 |

有配置时，未设置 imageStore 默认使用配置目录下的 `data/image-store/`。没有配置时，独立命令使用系统临时目录中的默认镜像库。层缓存未配置时使用临时目录下的默认层缓存。建议长期使用时显式指定这两个缓存路径。

程序升级时保留运行数据，程序分发包不包含下载镜像和构建缓存。旧 profile 的 `baseCacheDir` 应迁移到 `cache.imageStore`；内部缓存格式、迁移和清理边界见 [CAS 镜像库](../CAS_STORE.md)。

完整查询与迁移步骤见[打包台账与运行数据](HISTORY.md)。

台账默认保存在当前用户的 `~/.pyimagebuilder/build-records/`。可在配置顶层设置 `"buildRecordsDir": "./data/build-records"`，相对路径以配置文件所在目录为准；环境变量优先。建议放在 Dockerfile 构建上下文之外。每次成功打包保存一份小型 JSON，记录时间、标签、平台和产物信息，不保存应用包、鉴权参数或镜像层。换机器时可以单独备份、迁移此目录。

构建和查询需使用同一台账路径。台账写入失败时，命令报告错误并撤销本次产物；如明确不需要登记，可在构建或 Fast 命令加 `--no-build-records`。通过 Windows 的 `--wsl` 启动时由 Windows 端登记本机路径。
