# PyImageBuilder

**用 Python 把 WAR、dist.zip 或 Dockerfile 构建成镜像 tar，构建机器无需安装 Docker 或 Podman。**

支持 Docker archive 和 OCI Image Layout。Fast 面向 TongWeb/Tomcat 应用交付；普通构建读取 Dockerfile；图形操作可使用本机 Web 工作台。

## 选择使用方式

| 你的任务 | 入口 | 操作指南 |
| --- | --- | --- |
| 已有 WAR 或 dist.zip，立即打包 | `main.py fast` / 独立 `fast.py` | [Fast 打包](docs/guides/FAST.md) |
| 在浏览器选择文件、提交任务 | `main.py --web` | [Web 工作台](docs/guides/WEB.md) |
| 已有 Dockerfile | `main.py build` | [Dockerfile 构建](docs/guides/BUILD.md) |
| 查打包过的版本、找到交付物 | `main.py builds` | [打包台账与运行数据](docs/guides/HISTORY.md) |
| 下载、入库、导出或推送镜像 | `pull/load/save/push` | [命令速查](docs/guides/COMMANDS.md) |
| 将镜像合为一层并保留启动配置 | `flatten` | [rootfs 与扁平化](docs/ROOTFS_COMMANDS.md) |

## 环境与能力边界

| 场景 | 所需环境 |
| --- | --- |
| Fast、无 RUN 的 Dockerfile、镜像归档处理 | Windows 或 Linux（包括 UOS），Python 3.7 及以上；核心使用标准库 |
| 真正执行 Linux RUN | Linux，或 Windows 上已配置的 WSL；另需 namespace、OverlayFS 和相应权限 |
| 离线打包 | 已有本地基础 tar，或本工具镜像库已有匹配平台和来源的基础镜像 |
| 加载并启动交付镜像 | 目标机器安装 Docker 或其他兼容运行环境 |

Python 可以完成镜像归档构建；RUN 仍依赖 Linux 内核执行环境。指令与 flag 见 [Dockerfile 兼容表](docs/DOCKERFILE_COMPAT.md)。本项目是实验性构建器，最终镜像需在目标环境加载并启动应用验收。

## 首次准备

下载 [GitHub 源码](https://github.com/ZC-TZ/pyimagebuilder)并解压，或执行：

```text
git clone https://github.com/ZC-TZ/pyimagebuilder.git
cd pyimagebuilder
```

进入包含 `main.py` 的目录，再配置基础镜像。已有 `config.json` 时检查和编辑；尚未配置时，可复制 `config.example.json` 为 `config.json`，按[配置指南](docs/guides/CONFIGURATION.md)修改地址、账户、模板和输出目录。

模板名 `tongweb`、`tomcat` 只是选择名。基础镜像引用、Linux 账户、部署目录与服务配置必须符合实际镜像。示例地址和包路径均需替换。

## 最常用的打包命令

以下假定已配置好对应 profile，且输出文件尚不存在。

**UOS / Linux：**WAR 命令按所需模板二选一；前端生成另一份输出。

```bash
python3 main.py fast /data/packages/hisq.war hisq 1.0.0 --profile tongweb
python3 main.py fast /data/packages/hisq.war hisq 1.0.0 --profile tomcat
python3 main.py fast /data/packages/dist.zip hisq 1.0.0 --profile tomcat
```

**Windows / PowerShell：**

```powershell
python main.py fast D:/packages/hisq.war hisq 1.0.0 --profile tomcat
python main.py fast D:/packages/dist.zip hisq 1.0.0 --profile tomcat
```

| 输入 | 默认标签 | 默认文件名 |
| --- | --- | --- |
| WAR | `hisq_back:1.0.0` | `hisq_back-1.0.0.tar` |
| dist.zip | `hisq_front:1.0.0` | `hisq_front-1.0.0.tar` |

项目名与版本来自位置参数，输出目录来自模板的 `outputDir`；未设置时，CLI 使用应用包所在目录。可用 `-o 路径` 指定文件。独立运行时将 `main.py fast` 换成 `fast.py`，其余参数相同。

配置在当前目录时通常无需每次传 `--config`。存在多套配置时显式指定同一份配置；[配置查找顺序](docs/guides/CONFIGURATION.md#1-找到正在使用的配置)也适用于普通构建与 Web。

## 在浏览器操作

```bash
python3 main.py --web
```

Windows 使用 `python main.py --web`。在本机浏览器打开终端输出的完整会话链接；默认端口 8765，冲突时加 `--port 0`。

工作台支持 Fast、Dockerfile 构建、镜像库操作、台账、空间与配置管理。仓库密码需在**启动工作台的终端**设置环境变量；操作步骤见 [Web 指南](docs/guides/WEB.md)。

## 普通构建与交付

```bash
python3 main.py build /data/project --check
python3 main.py build /data/project -t hisq_back:1.0.0 -o /data/images/hisq_back-1.0.0.tar
python3 main.py verify /data/images/hisq_back-1.0.0.tar
```

未提供基础 tar/map 时，build 先查本地镜像库，缺失后按仓库配置拉取 FROM。`--offline` 禁止联网；`--pull` 刷新远端基础镜像。详见[构建指南](docs/guides/BUILD.md)。

把 Docker archive 复制到目标 Docker 机器后执行：

```text
docker load -i hisq_back-1.0.0.tar
```

本工具的 build/fast 生成归档并登记成功台账，**不会自动将结果注册到本工具镜像库，也不会加载到 Docker**。需要本工具按标签管理时执行 `python3 main.py load -i /data/images/hisq_back-1.0.0.tar`。

## 日常查询与升级

```bash
python3 main.py builds
python3 main.py builds --project hisq
python3 main.py images
python3 main.py system df
```

`builds` 查询成功构建记录，`images` 查询本工具镜像库，两者用途不同。台账位置及迁移见 [打包台账](docs/guides/HISTORY.md)。

更新程序时保留自己的 `config.json`、部署 XML、镜像库、缓存、台账与交付 tar；停止旧工作台，更新代码后重新启动。数据不应随源码更新包反复搬运。

## 查阅更多

- [完整文档目录](docs/README.md)：按任务找到使用指南和技术参考。
- [常见问题](docs/guides/TROUBLESHOOTING.md)：按报错和现象排查。
- [性能排查](docs/guides/PERFORMANCE.md)：区分下载、层准备、索引与导出耗时。
- [开发与维护](CONTRIBUTING.md)：测试方法、验收范围和提交约定。
