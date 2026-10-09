# 构建进度输出

[文档目录](README.md) · [项目首页](../README.md)

以下是当前 `PlainRenderer` 的输出格式示例。路径、镜像名和 digest 已缩写；RUN 和私有仓库示例展示相应事件的渲染格式，本机 Windows 环境未执行 Linux RUN 或连接内网仓库。

## 1. 完全命中层缓存

```text
PyBuilder | Building demo/app:1 [linux/amd64]
[+] Preparing build
    > Dockerfile parsed: 3 instruction(s)
    OK Preparing build (0.00s)
[1/3] FROM example/base:1
    > Base archive: C:\images\base.tar
    OK done (0.01s)
[2/3] COPY payload /app/payload
    CACHED sha256:1b2c...
    OK done (0.00s)
[3/3] ENV A=B
    OK metadata only (0.00s)
[+] Exporting image archive
    OK Exporting image archive (0.03s)
[+] Verifying output digest
    OK Verifying output digest (0.01s)
Build succeeded
Image:     demo/app:1
Platform:  linux/amd64
Output:    C:\images\app.tar
Digest:    sha256:db43...
Size:      41.0 KiB
Layers:    2
Cache:     1 hit(s), 0 miss(es), 0 stored
Duration:  0.07s
```

## 2. 包含 RUN 的构建

```text
[3/5] RUN echo ready && touch /app/ready
    CACHE MISS
    > ready
    OK done (1.21s)
```

RUN 的 stdout 和 stderr 都作为该步骤的 `step_log` 事件发出，JSON 事件的 `details.stream` 标明来源。

## 3. Fast 构建

```text
PyBuilder | Building sfcx_back:1.3.4 [linux/amd64]
[+] Resolving base image
    > Using local base D:\images\tongweb.tar
    OK Resolving base image (0.02s)
[+] Preparing application package
    OK Preparing application package (0.01s)
[+] Preparing build
    > Dockerfile parsed: 3 instruction(s)
    OK Preparing build (0.00s)
[1/3] FROM example/tongweb:11
[2/3] COPY --chown=tongadmin:tonggrp app.war /soft/TongWeb7.0/autodeploy/sfcx.war
[3/3] COPY --chown=tongadmin:tonggrp tongweb.xml /soft/TongWeb7.0/conf/tongweb.xml
[+] Exporting image archive
...
Build succeeded
Image:     sfcx_back:1.3.4
```

## 4. 首次下载基础镜像

```text
[+] Resolving base images
    > Resolving registry.example.com/team/base:1 (linux/amd64)
    > Registry layer 1/7
    Downloading Registry blob 72% (552.0 MiB/766.0 MiB)
    ... Resolving base images (8.0s)
    > Pulled base registry.example.com/team/base:1 (linux/amd64)
    OK Resolving base images (12.13s)
```

Artifactory 来源会显示 URL、下载文件进度、格式转换与打包状态；第二次构建会显示 `Cached base ...`。

## 5. RUN 失败

```text
[5/10] RUN yum install -y curl
    CACHE MISS
    > Error: package curl not found
    FAILED RUN failed: process exited with code 1 (Dockerfile:17, 3.21s)
Build failed at step 5/10: BuildError: RUN failed: process exited with code 1
Dockerfile:17
Duration: 3.24s
```

默认隐藏内部 Python traceback；使用 `--debug` 时打印完整 traceback。

## 6. 导出大镜像

```text
[+] Exporting image archive
    Writing Docker layer 72% (552.0 MiB/766.0 MiB)
    ... Exporting image archive (4.0s)
    Writing Docker layer 100% (766.0 MiB/766.0 MiB)
    OK Exporting image archive (5.02s)
[+] Verifying output digest
    Hashing archive 100% (766.0 MiB/766.0 MiB)
    OK Verifying output digest (0.82s)
Build succeeded
```

`--progress=json` 示例：

```jsonl
{"type":"step_start","elapsed":0.021,"phase":"COPY app.war /app/app.war","step":2,"total":3,"instruction":"COPY app.war /app/app.war","line":2}
{"type":"step_progress","elapsed":0.455,"message":"Copying app/app.war","phase":"COPY app.war /app/app.war","step":2,"total":3,"instruction":"COPY app.war /app/app.war","line":2,"current":10485760,"amount":20971520,"unit":"bytes"}
{"type":"step_success","elapsed":0.892,"message":"done","phase":"COPY app.war /app/app.war","step":2,"total":3,"instruction":"COPY app.war /app/app.war","line":2,"duration":0.871}
```

事件由 `progress.events.BuildEvent` 定义，`progress.reporter.BuildReporter` 发布；console、JSON 和未来的 Web UI 只需实现 `render(event)` / `close()` 接口。高频字节更新最多每 250 毫秒发一次，读取器每约 1 MiB 才调用一次进度回调。CI 和重定向输出不含颜色与回车覆盖符。
