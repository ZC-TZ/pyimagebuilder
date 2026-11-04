"""在整次构建期间保留缓存租约，避免清理删除已命中但还在使用的层。"""

import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from compat import is_linked_directory
from errors import BuildError
from store_lock import file_lock


_readers = threading.local()


def _enter_gate(stack, path, maintenance):
    # 并行构建只在登记槽位时相遇；允许短暂重试，不等待正在执行的维护任务。
    for attempt in range(12):
        try:
            stack.enter_context(file_lock(path, "Layer cache maintenance"))
            return
        except BuildError as exc:
            if maintenance or attempt == 11 or " is busy;" not in str(exc):
                raise
            time.sleep(0.01)


@contextmanager
def cache_activity(directory, maintenance=False):
    if directory is None:
        yield
        return
    slots = Path(directory).resolve() / "activity"
    local = getattr(_readers, "counts", None)
    if local is None:
        local = _readers.counts = {}
    if maintenance and local.get(slots):
        raise BuildError("Active layer cache build is busy; finish the build before cleaning")
    if is_linked_directory(slots):
        raise BuildError("Layer cache activity directory must not be a link")
    with ExitStack() as active:
        with ExitStack() as gate:
            _enter_gate(gate, slots / "gate.lock", maintenance)
            for number in range(128):
                slot = slots / ("build-{}.lock".format(number))
                if maintenance and not slot.exists() and not slot.is_symlink():
                    continue
                try:
                    active.enter_context(file_lock(slot, "Active layer cache build"))
                except BuildError as exc:
                    if maintenance or " is busy;" not in str(exc):
                        raise
                    continue
                if not maintenance:
                    break
            else:
                if not maintenance:
                    raise BuildError("All layer cache build slots are occupied")
            if maintenance:
                yield
                return
        local[slots] = local.get(slots, 0) + 1
        try:
            yield
        finally:
            local[slots] -= 1
            if not local[slots]:
                del local[slots]
