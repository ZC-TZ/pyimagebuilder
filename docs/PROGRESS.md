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

Fast 使用 Artifactory → CAS 时，大层处理中的字节进度仍按时间节流，但每层完成解包、摘要/tar 校验和解码层入库后，都会保留一条完成记录，快速小层也不会省略：

```text
转换镜像层 1/21 | 完成（解包、校验、入库） | 264.0 MiB | 5.20s
转换镜像层 2/21 | 完成（解包、校验、入库） | 8.0 MiB | 0.12s
...
转换镜像层 21/21 | 完成（解包、校验、入库） | 10.0 KiB | 0.01s
镜像层准备完成：21/21 层，基础镜像已写入 CAS
```

大小是解包后的 layer tar 字节数，单层耗时包含该层查找、解包、校验、解码层入库及临时文件清理；汇总在整张镜像的 CAS 引用导入与可用性检查成功后输出。失败层不输出完成记录，引用导入或可用性检查失败时不输出该汇总。JSON 模式中的这些记录是 `step_log` 事件，保持现有事件 schema。

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

RUN 的管道或首个 fork 创建失败时，回收此前已分配的描述符，并保留原始错误供报告。
启用日志转发和普通输出模式遵循同一规则；不会把后续清理错误当作最初的创建错误。

RUN 日志回调报错时，停止调用日志回调并继续排空 stdout/stderr，等待进程结束与日志线程收尾。
命令非零退出时优先报告 RUN 错误；命令成功但日志转发失败时也报告构建失败，使用 `--debug` 可查看原始日志异常。
此规则不等同于主线程收到 Ctrl+C 后的进程取消流程。

RUN 的 stdout/stderr 每次最多读取 64 KiB。普通短行保持原有输出形式；超长行或连续无换行输出
会分成多个日志事件，达到分段上限即可转发，UTF-8 字符跨段时不会被错误替换。
各分段在普通控制台中可能显示为多行，在 JSON 模式中是多个 step_log 事件。
事件边界不代表原始输出的行边界，stdout 与 stderr 的全局先后顺序也不作保证。
诊断管道保留量最多 256 KiB：超限时保留前后各 128 KiB，并插入截断提示，继续读取其余内容到 EOF。
这限制的是执行器内部缓冲；自定义日志回调若自行累积全部事件，仍需自行管理内存。

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
