"""在线程池等待退出之前，撤销本次操作尚未开始的任务。"""

from contextlib import contextmanager


@contextmanager
def cancel_pending_on_error(executor):
    """提供跟踪任务的 submit；调用方异常退出时取消所有仍在排队的任务。

    必须放在线程池的 with 内部，或作为同一个 with 的后一个上下文。
    已运行的任务不能由 Future.cancel 停止，仍由外层线程池等待结束，
    之后才能删除下载目录。逐次登记也覆盖提交到一半就失败的情况。
    """
    futures = []

    def submit(*args, **kwargs):
        future = executor.submit(*args, **kwargs)
        futures.append(future)
        return future

    try:
        yield submit
    except BaseException:
        # Python 3.7 没有 shutdown(cancel_futures=True)，显式取消排队任务。
        for future in futures:
            future.cancel()
        raise
