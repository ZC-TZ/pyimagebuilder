# 文件系统导出、导入与镜像扁平化

[文档目录](README.md) · [项目首页](../README.md)

这三个命令只处理本地数据，使用 Python 标准库，不需要 Docker、Podman、root 或 WSL。Windows 也能保留 tar 中的 Linux 属主、权限、链接和设备信息，因为文件系统没有被解包到宿主。支持 linux/amd64 和 linux/arm64，不执行镜像内程序。

## 如何选择

| 命令 | 输入 | 输出 | 运行配置 |
| --- | --- | --- | --- |
| `export` | 本地镜像引用或 Docker/单平台 OCI 镜像归档 | rootfs tar | 不包含 ENV、USER、ENTRYPOINT 等配置 |
| `import` | rootfs tar，或二进制 stdin | 新的单层 Docker/OCI 镜像归档 | 从空配置开始，可重复 `--change` 设置 |
| `flatten` | 本地镜像引用或 Docker/单平台 OCI 镜像归档 | 新的单层 Docker/OCI 镜像归档 | 默认完整保留原运行配置，可用 `--change` 覆盖 |

需要把现有 TongWeb/Tomcat 镜像打成单层时，直接用 **flatten**。它会保留 ENV、USER、WORKDIR、CMD、ENTRYPOINT、EXPOSE、VOLUME、LABEL、HEALTHCHECK、STOPSIGNAL、SHELL、ONBUILD 及其他运行字段，避免手工抄写遗漏。

`save/load` 处理已有镜像的 manifest/config/layers；`export/import` 处理文件系统，二者不能互换。`optimize` 尝试删除冗余整层，`flatten` 将整个可见文件树写成一层。单层不保证更小；被下层文件占用但已覆盖/删除的数据不会再进入合并层。

## 1. 推荐：构建后直接扁平化

在工具目录运行；以下假设 Dockerfile 和构建上下文位于 `D:\project`，输出放在 `D:\images`：

```powershell
python .\main.py build D:\project -t cmas_back:9.4 -o D:\images\cmas_back_9.4.tar
python .\main.py flatten D:\images\cmas_back_9.4.tar -t cmas_back:9.4-flat -o D:\images\cmas_back_9.4_flat.tar
```

最终 `D:\images\cmas_back_9.4_flat.tar` 是镜像归档，在有 Docker 的机器上可执行：

```text
docker load -i cmas_back_9.4_flat.tar
```

也可以直接处理已导入/拉取的镜像库引用：

```powershell
python .\main.py load -i D:\images\cmas_back_9.4.tar
python .\main.py flatten cmas_back:9.4 -t cmas_back:9.4-flat -o .\cmas_back_9.4_flat.tar
```

同一引用有多个来源时指定 `--source local`、`registry` 或 `artifactory`。CAS 输入直接读取已校验的 config 和层，不生成中间基础镜像 tar。未导入的 tar 适配缓存仍可读取。镜像库由现有 `--config`、`--image-store`、`cache.imageStore` 定位；命令不会自动联网拉取缺失镜像。

## 2. 单独导出文件系统

```powershell
python .\main.py export .\cmas_back_9.4.tar -o .\rootfs.tar
```

`rootfs.tar` 只包含最终可见文件树；whiteout 与 opaque 删除已应用，不再包含删除标记或已被覆盖的旧文件。这个文件**不能直接当作镜像交给 docker load**，需要先执行 import。

多标签归档用 `export --tag 输入标签`；flatten 则用 `--input-tag 输入标签` 选择输入、`-t/--tag` 设置输出。Docker 归档同一标签有多个平台时用 `--platform linux/arm64` 选择。OCI 输入沿用现有单平台、未压缩 OCI layout tar 的范围；多平台 index 需要先准备目标平台镜像。

这些命令没有容器实例，也没有运行时挂载数据。export 会保留镜像本身的文件，包括声明 VOLUME 的目录内原有内容，不导出任何宿主挂载或运行中容器的改动。[Docker export 参考](https://docs.docker.com/reference/cli/docker/container/export/)

## 3. 从 rootfs 导入新镜像

```powershell
python .\main.py import .\rootfs.tar cmas_back:9.4-flat -o .\imported.tar `
  --change 'ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/soft/TongWeb7.0/jdk1.8/bin:/soft/TongWeb7.0/bin' `
  --change 'ENV TMOUT=120 LANG=en_US.utf8 JAVA_HOME=/soft/TongWeb7.0/jdk1.8' `
  --change 'ENV SECURITY_ENABLED=true SECURITY_MINIMUMMASK=0002 SECURITY_CHECKEDUSERS=root UMASK=0022' `
  --change 'USER tongadmin' `
  --change 'WORKDIR /soft/TongWeb7.0' `
  --change 'EXPOSE 8089' `
  --change 'ENTRYPOINT ["bin/startserver.sh"]'
```

上面只演示如何设置配置；具体 PATH、用户、目录和端口应以实际基础镜像为准。rootfs 中需已有相应脚本及用户数据。`import` 不从原镜像找回丢失配置，需要保留配置时选择 flatten。

`--change`（简写 `-c`）支持：

```text
CMD ENTRYPOINT ENV EXPOSE HEALTHCHECK LABEL ONBUILD STOPSIGNAL USER VOLUME WORKDIR
```

- 复用项目 Dockerfile 解析器，ENV/LABEL 使用 `KEY=value`；不支持旧式 `ENV KEY value`。
- 按顺序执行变量替换，当前指令引用之前设置的 ENV。CMD/ENTRYPOINT 保留 shell 与 JSON exec 的区别；import 的默认 shell 是 `/bin/sh -c`，flatten 使用保留的 SHELL。
- 新 ENTRYPOINT 清除继承 CMD，除非本次 change 列表之前已明确设置 CMD。
- WORKDIR/VOLUME **只更新配置**，不会创建目录或增加额外文件层；ONBUILD 只登记触发器，后续真正构建 FROM 时才执行。
- RUN、COPY、ADD、ARG、FROM、SHELL 不允许作为 change；此功能仅修改元数据。[Docker import 配置指令](https://docs.docker.com/reference/cli/docker/image/import/)

import 接受 tar、tar.gz、tar.bz2、tar.xz；用 `--platform` 指定 rootfs 的实际架构，它不会自动转换程序架构。`-m/--message` 设置本次导入历史的 comment。输出标签省略版本时使用 `latest`，也支持 `harbor.internal:5000/team/app:1`。

## 4. 独立运行、管道和输出格式

独立脚本与 main.py 使用同一实现，在其他目录也可通过绝对脚本路径运行：

```powershell
python .\rootfs_archive.py export .\app.tar -o .\rootfs.tar
python .\rootfs_archive.py import .\rootfs.tar app:flat -o .\flat.tar
python .\rootfs_archive.py flatten .\app.tar -t app:flat -o .\flat.tar
```

export 省略 `-o` 或用 `-o -` 时写二进制 stdout；import 用 `-` 读二进制 stdin。Linux shell 示例：

```sh
python main.py export app:1 | python main.py import - app:flat -o flat.tar --change 'CMD ["start"]'
```

进度全部写 stderr，不混入 tar 或 stdout JSON。Windows PowerShell 各版本的原生二进制管道处理不同，优先使用前面的两个文件命令。二进制流输出失败后，接收端需丢弃已收到的部分内容。

import/flatten 的 `-o` 必须是新文件路径，不能是 `-`；输出默认 Docker archive，用 `--format oci` 输出 OCI layout tar。与当前 build 一样，生成归档不自动注册到本工具镜像库；需要后续以引用操作时执行 `load -i 文件.tar`，OCI 用 `cas import 文件.oci.tar 标签 --format oci`。

## 5. 元数据、身份与失败处理

合并保留 mode、UID/GID、uname/gname、mtime、符号链接、硬链接 inode 分组、FIFO、设备号和 PAX xattr。硬链接原目标被覆盖或删除时，旧 inode 内容仍保留。隐式父目录使用 uid/gid 0、mode 0755、mtime 0；稀疏文件内容展开为普通文件，不保留稀疏磁盘分配布局。

rootfs 导入拒绝绝对/越界路径、反斜杠、重复规范化路径、通过非目录/符号链接父路径写入内容、无效硬链接及不支持的成员类型。同层包含非目录父路径及其子路径时，在应用前按统一规则拒绝，避免先读取子文件、后用父文件覆盖时丢数据或抛出未捕获异常。真实名为 `.wh.*` 的文件无法直接表示为 OCI 文件层，明确报错。全程没有向宿主提取 tar 成员。

flatten 确实改变了层链，会更新 DiffID/config/ImageID 和 manifest，原 history 改为一条合并记录。原镜像文件和引用保持不变。运行配置与额外 config 字段保留；OCI 镜像级、config descriptor 与 layout index 的扩展元数据保留，原层注解不转嫁给合并层。旧 SBOM/provenance/签名的摘要绑定不适用于新镜像，需要重新生成。

新配置和外层归档时间使用 `--source-date-epoch`，缺省取 SOURCE_DATE_EPOCH 或 0；文件 mtime 保留输入值。同样的输入、标签、配置变更、格式和 epoch 得到相同产物。输出先写私有临时目录并验证，再排他发布；失败清理本次临时文件，不覆盖已有或竞争创建的输出。

查看完整参数：`python main.py export --help`、`import --help`、`flatten --help`。

## 硬链接与权限

export 与 flatten 以可见 inode 分组输出硬链接，使用 inode 的最终 UID/GID、mode、mtime 与 PAX 扩展属性。硬链接头更新权限时，同 inode 的所有名字一起更新；原路径被后续层覆盖或删除后，存活的别名仍保留原内容及权限。同一层被重复应用时，重新写入的普通文件是新 inode，不与旧别名重新合并。

上述行为参照 [containerd 的层解包实现](https://github.com/containerd/containerd/blob/main/pkg/archive/tar.go)。权限更新中的 chown 会清除旧的 security.capability，除非当前链接头显式重新设置该属性；归档保留能力与真实 Linux RUN 能否物化该属性须分别验证。
