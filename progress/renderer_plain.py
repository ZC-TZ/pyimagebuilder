"""为 CI 和重定向日志提供稳定的逐行控制台输出。"""

import sys

from .progress_bar import progress_text, size
from .renderer_base import Renderer


class PlainRenderer(Renderer):
    """把构建事件渲染为适合重定向保存的日志行。"""
    def __init__(self, stream=None, quiet=False, verbose=False):
        self.stream = stream or sys.stdout
        self.quiet = quiet
        self.verbose = verbose

    def _line(self, value):
        print(value, file=self.stream, flush=True)

    def render(self, event):
        """以稳定的逐行格式输出当前事件。"""
        kind = event.type
        if self.quiet and kind not in ("build_success", "build_failed"):
            return
        if kind == "build_start":
            self._line("PyBuilder | Building {} [{}]".format(
                event.details.get("image", "image"), event.details.get("platform", "unknown")))
        elif kind == "phase_start":
            self._line("[+] {}".format(event.message))
        elif kind == "phase_success":
            self._line("    OK {} ({:.2f}s)".format(event.message or event.phase, event.duration or 0))
        elif kind == "step_start":
            instruction = event.instruction or ""
            if "\n" in instruction:
                instruction = instruction.splitlines()[0] + " ..."
            self._line("[{}/{}] {}".format(event.step, event.total, instruction))
        elif kind == "step_cache_hit":
            self._line("    CACHED {}".format(event.message))
        elif kind == "step_cache_miss":
            self._line("    CACHE MISS")
        elif kind == "step_log":
            self._line("    > {}".format(event.message))
        elif kind == "step_progress":
            self._line("    {}".format(progress_text(event)))
        elif kind == "heartbeat":
            self._line("    ... {} ({:.1f}s)".format(event.message, event.duration or 0))
        elif kind == "step_success":
            suffix = " ({:.2f}s)".format(event.duration or 0)
            self._line("    OK {}{}".format(event.message or "done", suffix))
        elif kind == "step_failed":
            self._line("    FAILED {} (Dockerfile:{}, {:.2f}s)".format(
                event.message, event.line or "?", event.duration or 0))
        elif kind == "build_failed":
            label = " at step {}/{}".format(event.step, event.total) if event.step else ""
            print("Build failed{}: {}".format(label, event.message), file=sys.stderr, flush=True)
            if event.line:
                print("Dockerfile:{}".format(event.line), file=sys.stderr, flush=True)
            print("Duration: {:.2f}s".format(event.duration or 0), file=sys.stderr, flush=True)
        elif kind == "build_success":
            summary = event.details
            self._line("Build succeeded")
            for name, key in (("Image", "image"), ("Platform", "platform"),
                              ("Output", "output"), ("Digest", "digest"),
                              ("Size", "size"), ("Layers", "layers"),
                              ("Cache", "cache")):
                value = summary.get(key)
                if value is not None:
                    self._line("{:<10} {}".format(name + ":", size(value) if key == "size" else value))
            for key in ("sbom", "provenance", "signature", "oci_output"):
                if summary.get(key):
                    self._line("{:<10} {}".format(key.capitalize() + ":", summary[key]))
            if summary.get("timings"):
                self._line("Stage timings (seconds):")
                for label, seconds in summary["timings"].items():
                    self._line("  {:<29} {:>8.2f}".format(label, seconds))
            self._line("Duration:  {:.2f}s".format(event.duration or 0))
