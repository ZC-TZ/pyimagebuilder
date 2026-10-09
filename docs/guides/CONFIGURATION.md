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

JSON 不允许注释或尾随逗号。请使用模板里的真实字段名，不要把说明文字写入配置。

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

在运行命令的终端设置密码：

```powershell
$env:PYIMAGEBUILDER_ARTIFACTORY_PASSWORD = '实际密码'
$env:PYIMAGEBUILDER_REGISTRY_PASSWORD = '实际密码'
```

`passwordEnv` 存的是变量名，程序运行时读取其值。JSON 中不允许 `password` 或 `token` 字段。连接参数通常可用相应命令的 `--username`、`--password-env`、`--ca-file` 覆盖；Fast 日常命令使用 profile 对应的 repositories 设置。

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

有配置时，未设置 imageStore 默认使用配置目录下的 `data/image-store/`。没有配置时，独立命令使用系统临时目录中的默认镜像库。层缓存未配置时使用临时目录下的默认层缓存。建议长期使用时显式指定这两个缓存路径。

程序升级时保留运行数据，程序分发包不包含下载镜像和构建缓存。旧 profile 的 `baseCacheDir` 应迁移到 `cache.imageStore`；内部缓存格式、迁移和清理边界见 [CAS 镜像库](../CAS_STORE.md)。
