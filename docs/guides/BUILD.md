# 使用 Dockerfile 构建

[文档目录](../README.md) · [项目首页](../../README.md) · [Dockerfile 兼容表](../DOCKERFILE_COMPAT.md)

普通构建的基本形式是：

```powershell
python .\main.py build D:\project -t company/app:1 -o D:\images\app.tar
```

`D:\project` 是构建上下文，默认读取其中的 `Dockerfile`。`-t` 指定镜像标签，`-o` 指定输出归档；本工具默认写文件，不会把结果加载到 Docker daemon，也不会自动注册到本地镜像库。

## 1. 准备构建目录

```text
D:\project\
├── Dockerfile
└── app.war
```

例如，Tomcat 部署：

```dockerfile
FROM harbor.internal/project/tomcat:9
COPY --chown=tomcat:tomcat app.war /soft/tomcat/webapps/app.war
```

请替换基础镜像地址、账户和部署目录。`COPY` 源路径相对构建上下文，目标路径属于镜像文件系统。输出 tar、输入锁和交付证明建议放在上下文之外。

先做静态检查：

```powershell
python .\main.py build D:\project --check
```

检查支持的语法与部分本地输入边界，不下载镜像，不执行 RUN，也不能证明基础镜像可访问或应用可运行。`.dockerignore` 当前只允许空行和注释，实际忽略规则会报错；详见兼容表。

## 2. 选择基础镜像来源

### 自动查库与拉取

```powershell
python .\main.py build D:\project -t company/app:1 -o D:\images\app.tar
```

未提供本地基础输入时：**查找本地镜像库 → 缺失后拉取远端 → 构建**。自动拉取使用完整镜像引用，如 `harbor.internal/project/tomcat:9`；不能假定 `FROM tomcat` 自动解析成 Docker Hub 官方镜像。仓库鉴权和来源见 [统一配置](CONFIGURATION.md)。下载失败会终止构建。

| 参数 | 使用时机 |
| --- | --- |
| `--offline` | 只用本地输入/已缓存镜像；缺失就报错，也禁止远程 ADD |
| `--pull` | 刷新远端 FROM 标签；不能与 offline 或显式 base tar/map 合用 |
| `--pull-source artifactory` | 明确使用 Artifactory 下载器；也可在 repositories 配置来源 |
| `--image-store D:\builder-data\image-store` | 指定共享镜像库，覆盖配置路径 |

### 使用已有 Docker save tar

```powershell
python .\main.py build D:\project --base-tar D:\images\tomcat-base.tar `
  -t company/app:1 -o D:\images\app.tar --offline
```

基础 tar 中的标签应对应 Dockerfile 的 FROM。`FROM scratch` 无需基础 tar；只有无 RUN 的文件打包和元数据指令时，可在 Windows 原生执行。

### 多个基础镜像与多阶段

需要显式提供多个基础镜像时，创建 `D:\builder-config\base-map.json`：

```json
{
  "company/build-base:1": "D:/images/build-base.tar",
  "company/runtime-base:1": "D:/images/runtime-base.tar"
}
```

键对应 FROM 引用，值使用实际本地 tar 路径：

```powershell
python .\main.py build D:\project --base-map D:\builder-config\base-map.json `
  -t company/app:1 -o D:\images\app.tar --offline
```

Dockerfile 可以使用 `FROM ... AS stage` 和 `COPY --from=stage`；`--target stage` 只构建到指定阶段。不提供 base-map 时，也可按各 FROM 自动查库/拉取。`--base-tar` 与 `--base-map` 互斥。

## 3. 常用构建选项

| 参数 | 含义 |
| --- | --- |
| `-f D:\project\Dockerfile.release` | 使用另一份 Dockerfile |
| `--build-arg NAME=VALUE` | 设置构建 ARG，可重复；不能用于传密码 |
| `--platform linux/arm64` | 选择目标平台，默认 linux/amd64 |
| `--target runtime` | 指定目标阶段 |
| `--cache-dir D:\builder-data\layers` | 指定指令层缓存 |
| `--no-cache` | 禁止指令缓存读写，不等于刷新 FROM |
| `--verify-cache` | 对复用的缓存进行完整内容校验 |
| `--source-date-epoch 0` | 固定新增内容的时间输入，支持可重复构建 |
| `--progress plain` | 逐行输出，适合日志；json 输出结构化事件 |
| `--debug` | 出错时显示 traceback |

有 RUN 的 Dockerfile 必须加 `--run` 并具备 Linux 执行条件；Windows 可加 `--wsl`。真实执行、网络和 rootless 条件见 [进阶构建](ADVANCED.md#执行-run)。

## 4. 选择输出格式

| 格式 | 命令选项 | 产物 |
| --- | --- | --- |
| Docker archive（默认） | 不需要额外选项 | `-o` 指定的文件，可供 docker load |
| OCI Image Layout tar | `--format oci` | `-o` 指定的 OCI 归档 |
| 两种格式 | `--format both` | Docker 文件和同目录 `<输出文件名去扩展名>.oci.tar` |

```powershell
python .\main.py build D:\project -t company/app:1 -o D:\images\app.tar `
  --format both --oci-output D:\images\app.oci.tar
```

OCI archive 是 OCI layout 的 tar 包，不能把它与普通 rootfs tar 混淆，也不应假定任意旧版 Docker 的 load 都支持它。

## 5. 检查、入库与交付

```powershell
python .\main.py verify D:\images\app.tar
python .\main.py inspect D:\images\app.tar
python .\main.py load -i D:\images\app.tar
```

`load` 将镜像注册到 **PyImageBuilder 的本地镜像库**，便于后续按引用使用。交付给 Docker 时，在目标机器执行 `docker load -i app.tar`；与本工具的 load 作用位置不同。

下一步：[命令速查](COMMANDS.md) · [常见问题](TROUBLESHOOTING.md) · [交付证明与封闭构建](ADVANCED.md)
