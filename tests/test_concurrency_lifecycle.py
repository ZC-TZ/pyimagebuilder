"""并发与资源生命周期专项：真实线程、锁、Future 和文件，网络仅用本地替身。"""

import contextlib
import gc
import io
import os
import socket
import sys
import tempfile
import threading
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifactory_download as downloader
import store_lock
import registry
import main
import fast
import image_store
from errors import BuildError
from progress.reporter import BuildReporter
from registry import RegistryClient


class Renderer:
    def __init__(self, failed=None):
        self.failed = failed
        self.failure = BrokenPipeError("test output unavailable")
        self.arrived = threading.Event()
        self.closes = 0
        self.events = []

    def render(self, event):
        self.events.append(event.type)
        if event.type == self.failed:
            self.arrived.set()
            raise self.failure

    def close(self):
        self.closes += 1


def cleanup_reporter(reporter, threads=()):
    """旧提交复现也必须关闭遗留线程，避免污染后续测试的线程数。"""
    reporter.stop.set()
    for thread in set(threads) | ({reporter.thread} if reporter.thread else set()):
        if thread.ident is not None:
            thread.join(5)
    if hasattr(reporter, "close"):
        reporter.close()


class ConcurrencyLifecycleTests(unittest.TestCase):
    def test_artifactory_credentials_in_urls_are_rejected_without_echoing_them(self):
        for authority in ("test-user:test-sensitive@example.test", "test-user@example.test", "@example.test"):
            url = "https://" + authority + "/artifactory/repo/image/1/"
            for entry in (downloader.artifact_directory_url, downloader.make_opener):
                with self.subTest(authority=authority, entry=entry.__name__), \
                        patch.object(downloader, "build_opener") as create:
                    with self.assertRaises(ValueError) as failure:
                        entry(url)
                    self.assertNotIn(authority, str(failure.exception))
                    create.assert_not_called()
            opener = type("Opener", (), {"artifactory_origin": ("https", authority)})()
            with self.subTest(entry="request", authority=authority), self.assertRaises(ValueError):
                downloader.request(opener, url)

    def test_unused_reference_locks_are_reclaimed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(120):
                with store_lock.file_lock(root / (str(index) + ".lock")):
                    pass
            gc.collect()
            prefix = os.path.normcase(str(root.resolve()))
            self.assertFalse([key for key in store_lock._locks if key.startswith(prefix)])
            self.assertEqual(len(list(root.glob("*.lock"))), 120, "Kernel lock files must remain stable")

    def test_live_reference_lock_stays_shared_across_gc_and_threads(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ref.lock"

            def competitor():
                with self.assertRaises(BuildError):
                    with store_lock.file_lock(path):
                        self.fail("another thread entered the locked reference")

            with store_lock.file_lock(path), ThreadPoolExecutor(max_workers=1) as executor:
                key = os.path.normcase(str(path.resolve()))
                first = store_lock._locks[key]
                gc.collect()
                with store_lock.file_lock(path):
                    self.assertIs(store_lock._locks[key], first)
                    executor.submit(competitor).result(timeout=5)

    def test_final_render_failure_always_stops_heartbeat(self):
        for event in ("build_success", "build_failed"):
            with self.subTest(event=event):
                renderer = Renderer(event)
                reporter = BuildReporter(renderer, heartbeat_interval=0.01)
                reporter.start("test:1", "linux/amd64")
                try:
                    with self.assertRaises(BrokenPipeError) as raised:
                        if event == "build_success":
                            reporter.success()
                        else:
                            reporter.fail(BuildError("failed"))
                    self.assertIs(raised.exception, renderer.failure)
                    self.assertTrue(reporter.stop.is_set())
                    self.assertTrue(reporter.finished)
                    self.assertFalse(reporter.thread.is_alive())
                    self.assertEqual(renderer.closes, 1)
                finally:
                    cleanup_reporter(reporter)

    def test_background_render_failure_reaches_main_thread(self):
        renderer = Renderer("heartbeat")
        reporter = BuildReporter(renderer, heartbeat_interval=0.01)
        thread_type = threading.Thread
        escaped = []

        def factory(target, **kwargs):
            def guarded():
                try:
                    target()
                except BaseException as exc:
                    escaped.append(exc)
            return thread_type(target=guarded, **kwargs)

        try:
            with patch("progress.reporter.threading.Thread", side_effect=factory):
                reporter.start("test:1", "linux/amd64")
            reporter.phase_start("waiting")
            self.assertTrue(renderer.arrived.wait(5))
            reporter.thread.join(5)
            with self.assertRaises(BrokenPipeError) as raised:
                reporter.log("later")
            self.assertIs(raised.exception, renderer.failure)
            self.assertEqual(escaped, [])
        finally:
            cleanup_reporter(reporter)

    def test_repeated_start_cannot_create_an_untracked_thread(self):
        reporter = BuildReporter(Renderer())
        threads = []
        thread_type = threading.Thread

        def factory(**kwargs):
            thread = thread_type(**kwargs)
            threads.append(thread)
            return thread

        try:
            with patch("progress.reporter.threading.Thread", side_effect=factory):
                reporter.start("test:1", "linux/amd64")
                with self.assertRaises(RuntimeError):
                    reporter.start("test:2", "linux/amd64")
            self.assertEqual(len(threads), 1)
        finally:
            cleanup_reporter(reporter, threads)

    def test_heartbeat_pause_is_nested_and_resumes_after_exception(self):
        renderer = Renderer()
        reporter = BuildReporter(renderer)
        reporter.start("test:1", "linux/amd64")
        previous = reporter.thread
        pause = getattr(reporter, "pause_heartbeat", contextlib.nullcontext)
        try:
            with self.assertRaisesRegex(ValueError, "body failed"):
                with pause():
                    self.assertFalse(previous.is_alive())
                    with pause():
                        reporter.log("RUN log still works")
                        self.assertFalse(reporter.thread.is_alive())
                    self.assertFalse(reporter.thread.is_alive())
                    raise ValueError("body failed")
            self.assertTrue(reporter.thread.is_alive())
            self.assertIsNot(reporter.thread, previous)
            reporter.success()
            reporter.close()
            self.assertEqual(renderer.closes, 1)
        finally:
            cleanup_reporter(reporter)

    def test_start_inside_pause_waits_until_pause_ends(self):
        reporter = BuildReporter(Renderer())
        try:
            with reporter.pause_heartbeat():
                reporter.start("test:1", "linux/amd64")
                self.assertIsNone(reporter.thread)
            self.assertTrue(reporter.thread.is_alive())
        finally:
            cleanup_reporter(reporter)

    def test_failed_heartbeat_resume_preserves_body_error(self):
        for primary in (BuildError("RUN failed"), KeyboardInterrupt()):
            with self.subTest(primary=type(primary).__name__):
                reporter = BuildReporter(Renderer())
                reporter.start("test:1", "linux/amd64")
                try:
                    with patch("progress.reporter.threading.Thread.start",
                               side_effect=RuntimeError("cannot start heartbeat")):
                        with self.assertRaises(type(primary)) as raised:
                            with reporter.pause_heartbeat():
                                raise primary
                    self.assertIs(raised.exception, primary)
                    self.assertTrue(reporter.stop.is_set())
                    self.assertEqual(reporter._paused, 0)
                finally:
                    cleanup_reporter(reporter)

    def test_close_failure_preserves_active_error_but_is_not_silently_successful(self):
        class BrokenClose(Renderer):
            def close(self):
                super().close()
                raise OSError("close output failed")

        for operation in ("start", "success", "fail", "finally"):
            with self.subTest(operation=operation):
                event = {"start": "build_start", "success": "build_success",
                         "fail": "build_failed"}.get(operation)
                renderer = BrokenClose(event)
                reporter = BuildReporter(renderer)
                primary = renderer.failure if event else KeyboardInterrupt()
                try:
                    with self.assertRaises(type(primary)) as raised:
                        if operation == "start":
                            reporter.start("test:1", "linux/amd64")
                        else:
                            reporter.start("test:1", "linux/amd64")
                            if operation == "success":
                                reporter.success()
                            elif operation == "fail":
                                reporter.fail(BuildError("RUN failed"))
                            else:
                                try:
                                    raise primary
                                finally:
                                    reporter.close()
                    self.assertIs(raised.exception, primary)
                    self.assertTrue(reporter.finished)
                    self.assertEqual(renderer.closes, 1)
                    if reporter.thread is not None:
                        self.assertFalse(reporter.thread.is_alive())
                finally:
                    cleanup_reporter(reporter)
        reporter = BuildReporter(BrokenClose())
        with self.assertRaisesRegex(OSError, "close output failed"):
            reporter.close()


    def test_retry_after_interrupted_resume_cannot_revive_old_heartbeat(self):
        reporter = BuildReporter(Renderer(), heartbeat_interval=0.01)
        threads = []
        thread_type = threading.Thread
        arrived, resume = threading.Event(), threading.Event()
        interrupted = KeyboardInterrupt()

        class DelayedThread(thread_type):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.wait_started = self._started.wait

                def interrupted_wait(*_args, **_kwargs):
                    if not arrived.wait(5):
                        raise AssertionError("native heartbeat bootstrap did not arrive")
                    raise interrupted
                self._started.wait = interrupted_wait

            def _bootstrap(self):
                arrived.set()
                resume.wait(5)
                super()._bootstrap()

        def factory(**kwargs):
            kind = DelayedThread if len(threads) == 1 else thread_type
            thread = kind(**kwargs)
            threads.append(thread)
            return thread

        try:
            with patch("progress.reporter.threading.Thread", side_effect=factory):
                reporter.start("test:1", "linux/amd64")
                with self.assertRaises(KeyboardInterrupt) as failure:
                    with reporter.pause_heartbeat():
                        pass
                self.assertIs(failure.exception, interrupted)
                self.assertIsNone(threads[1].ident)
                # 调用方处理启动失败后再次暂停/恢复；旧代必须保持永久停止。
                with reporter.pause_heartbeat():
                    pass
            self.assertEqual(len(threads), 3)
            self.assertIs(reporter.thread, threads[2])
            resume.set()
            self.assertTrue(threads[1].wait_started(5))
            threads[1].join(0.2)
            self.assertFalse(threads[1].is_alive(), "retry revived an untracked heartbeat generation")
            self.assertTrue(threads[2].is_alive())
            reporter.log("reporter still works after retry")
        finally:
            resume.set()
            if len(threads) > 1:
                threads[1].wait_started(5)
            cleanup_reporter(reporter, threads)

    def test_concurrent_terminal_calls_emit_only_one_result(self):
        renderer = Renderer()
        reporter = BuildReporter(renderer)
        reporter.start("test:1", "linux/amd64")
        finishing = threading.Event()
        release = threading.Event()
        close = reporter.close
        main_thread = threading.current_thread()

        def delayed_close():
            if threading.current_thread() is not main_thread:
                finishing.set()
                if not release.wait(5):
                    raise AssertionError("terminal call was not released")
            close()

        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                with patch.object(reporter, "close", side_effect=delayed_close):
                    done = executor.submit(reporter.success)
                    self.assertTrue(finishing.wait(5))
                    reporter.fail(BuildError("late failure"))
                    release.set()
                    done.result(timeout=5)
                self.assertEqual([kind for kind in renderer.events if kind.startswith("build_")],
                                 ["build_start", "build_success"])
                self.assertEqual(renderer.closes, 1)
            finally:
                release.set()
                cleanup_reporter(reporter)

    def cli_reporter_cleanup(self, command):
        reporter = BuildReporter(Renderer())
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context = root / "context"
                context.mkdir()
                (context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
                config = root / "settings.json"
                config.write_text('{"schemaVersion":2}', encoding="utf-8")
                failure = KeyboardInterrupt()
                with patch("progress.make_reporter", return_value=reporter), \
                        patch.object(main, "make_reporter", return_value=reporter):
                    if command == "pull":
                        with patch.object(image_store, "pull_image", side_effect=failure):
                            with self.assertRaises(KeyboardInterrupt):
                                image_store.pull_main(["example.test/p/app:1", "--config", str(config)])
                    elif command == "fast":
                        with patch.object(fast, "quick_build", side_effect=failure):
                            with self.assertRaises(KeyboardInterrupt):
                                fast.main([str(root / "app.war"), "app", "1"])
                    else:
                        args = ["build", str(context), "--config", str(config), "--base-tar", str(root / "base.tar"),
                                "-t", "test:1", "-o", str(root / "image.tar")]
                        if command == "wsl":
                            with patch.object(main, "_run_wsl", return_value=17):
                                self.assertEqual(main.main(args + ["--wsl"]), 17)
                        else:
                            with patch.object(main, "build", side_effect=failure):
                                with self.assertRaises(KeyboardInterrupt):
                                    main.main(args)
                self.assertTrue(reporter.finished)
                self.assertFalse(reporter.thread.is_alive())
                self.assertEqual(reporter.renderer.closes, 1)
        finally:
            cleanup_reporter(reporter)

    def test_build_cli_interrupt_closes_reporter(self):
        self.cli_reporter_cleanup("build")

    def test_fast_cli_interrupt_closes_reporter(self):
        self.cli_reporter_cleanup("fast")

    def test_pull_cli_interrupt_closes_reporter(self):
        self.cli_reporter_cleanup("pull")

    def test_wsl_early_return_closes_reporter(self):
        self.cli_reporter_cleanup("wsl")

    def test_registry_concurrent_401_refreshes_only_once(self):
        for initial in (None, "expired"):
            with self.subTest(initial=initial):
                client = RegistryClient("example.test/p/app:1", "test-user", "test-password")
                scope = client._scope("pull")
                if initial:
                    client.tokens[scope] = initial
                gate = threading.Barrier(6)
                refreshes = []
                lock = threading.Lock()
                stale = "Bearer expired" if initial else client._basic()

                def single(_method, url, headers, *_args):
                    if "/token" in url:
                        with lock:
                            refreshes.append(url)
                            token = "fresh-" + str(len(refreshes))
                        return 200, {}, ('{"token":"' + token + '"}').encode()
                    if headers.get("Authorization") == stale:
                        gate.wait(5)
                        return 401, {"www-authenticate": 'Bearer realm="https://example.test/token"'}, b""
                    return 200, {}, headers["Authorization"].encode()

                with patch.object(client, "_single", side_effect=single), ThreadPoolExecutor(max_workers=6) as executor:
                    futures = [executor.submit(client.request, "GET", client._url("/blobs/test"), scope)
                               for _ in range(6)]
                    results = [future.result(timeout=5)[2] for future in futures]
                self.assertEqual(len(refreshes), 1)
                self.assertEqual(results, [b"Bearer fresh-1"] * 6)

    def test_registry_explicit_sensitive_headers_do_not_follow_cdn_redirect(self):
        client = RegistryClient("example.test/p/app:1")
        requests = []

        def single(_method, url, headers, *_args):
            requests.append((url, headers))
            if urlsplit(url).netloc == "example.test":
                return 307, {"location": "https://cdn.test/blob"}, b""
            return 200, {}, b"ok"

        with patch.object(client, "_single", side_effect=single):
            client.request("GET", client._url("/blobs/test"), client._scope("pull"),
                           headers={"aUthOriZaTion": "test-token", "Cookie": "test-cookie",
                                    "Proxy-Authorization": "test-proxy", "Accept": "blob"})
        self.assertEqual(len(requests), 2)
        foreign = {key.lower(): value for key, value in requests[1][1].items()}
        self.assertFalse(set(foreign) & {"authorization", "cookie", "proxy-authorization"})
        self.assertEqual(foreign["accept"], "blob")

    def test_registry_automatic_auth_replaces_case_variant_header(self):
        client = RegistryClient("example.test/p/app:1", "test-user", "test-password")
        seen = []
        with patch.object(client, "_single", side_effect=lambda _method, _url, headers, *_args:
                          (seen.append(headers) or (200, {}, b"ok"))):
            client.request("GET", client._url("/blobs/test"), client._scope("pull"),
                           headers={"authorization": "obsolete"})
        self.assertEqual([value for key, value in seen[0].items() if key.lower() == "authorization"],
                         [client._basic()])

    def test_registry_cancelled_auth_waiter_never_sends_request(self):
        client = RegistryClient("example.test/p/app:1")
        arrived = threading.Event()

        def request():
            arrived.set()
            return client.request("GET", client._url("/blobs/test"), client._scope("pull"))

        with patch.object(client, "_single") as single, ThreadPoolExecutor(max_workers=1) as executor:
            with client._auth_lock:
                done = executor.submit(request)
                self.assertTrue(arrived.wait(5))
                client.cancel()
            with self.assertRaises(CancelledError):
                done.result(timeout=5)
            single.assert_not_called()

    def test_registry_auth_retry_replaces_all_authorization_case_variants(self):
        for name in ("authorization", "aUthOriZaTion"):
            with self.subTest(header=name):
                client = RegistryClient("example.test/p/app:1")
                seen = []

                def single(_method, url, headers, *_args):
                    if "/token" in url:
                        return 200, {}, b'{"token":"new-token"}'
                    seen.append(headers)
                    if len(seen) == 1:
                        return 401, {"www-authenticate": 'Bearer realm="https://example.test/token"'}, b""
                    return 200, {}, b"ok"

                with patch.object(client, "_single", side_effect=single):
                    client.request("GET", client._url("/blobs/test"), client._scope("pull"),
                                   headers={name: "obsolete", "Accept": "blob"})
                self.assertEqual([value for key, value in seen[1].items()
                                  if key.lower() == "authorization"], ["Bearer new-token"])
                self.assertEqual(seen[1]["Accept"], "blob")

    def test_registry_cancel_at_response_end_cannot_return_success(self):
        for point in ("last_progress", "stream_eof", "json_read"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temporary:
                client = RegistryClient("example.test/p/app:1")

                class Response(io.BytesIO):
                    status = 200

                    def getheaders(self):
                        return [("Content-Length", "4")]

                    def read(self, size=-1):
                        result = super().read(size)
                        if point == "json_read" or (point == "stream_eof" and not result):
                            client.cancel()
                        return result

                response = Response(b"test")

                class Connection:
                    closed = False

                    def request(self, *_args, **_kwargs):
                        pass

                    def getresponse(self):
                        return response

                    def close(self):
                        self.closed = True
                        response.close()

                connection = Connection()
                partial = Path(temporary) / "partial"
                if point == "last_progress":
                    client.progress = lambda *_args: client.cancel()
                kwargs = {} if point == "json_read" else {"sink": partial, "max_stream_bytes": 4}
                with patch.object(registry.http.client, "HTTPSConnection", return_value=connection):
                    with self.assertRaises(CancelledError):
                        client.request("GET", client._url("/blobs/test"), client._scope("pull"), **kwargs)
                self.assertTrue(connection.closed)
                self.assertTrue(response.closed)
                if partial.exists():
                    partial.unlink()  # Windows 上确认写入句柄已经释放。

    def test_registry_incomplete_http_response_releases_real_socket(self):
        for failure in ("cancel", "blob_size", "json_size"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                reader, writer = socket.socketpair()
                responses = []
                connection = registry.http.client.HTTPConnection("example.test")
                connection.sock = reader

                class ObservedResponse(registry.http.client.HTTPResponse):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        responses.append(self)

                connection.response_class = ObservedResponse
                client = RegistryClient("example.test/p/app:1", insecure_http=True)
                if failure == "cancel":
                    client.progress = lambda *_args: client.cancel()
                kwargs = ({} if failure == "json_size" else
                          {"sink": Path(temporary) / "partial",
                           "max_stream_bytes": 4 if failure == "blob_size" else 12})
                try:
                    # HTTP/1.0 响应由 makefile 持有 socket；getresponse 会先关闭连接对象。
                    writer.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 12\r\n\r\nabcdefghijkl")
                    with patch.object(registry.http.client, "HTTPConnection", return_value=connection), \
                            patch.object(registry, "BUFFER", 4), patch.object(registry, "MAX_JSON", 4):
                        with self.assertRaises(CancelledError if failure == "cancel" else BuildError):
                            client.request("GET", client._url("/blobs/test"), client._scope("pull"), **kwargs)
                    self.assertEqual(len(responses), 1)
                    self.assertTrue(responses[0].isclosed(), "unread response still owns socket makefile")
                    self.assertEqual(reader.fileno(), -1, "closing connection did not release response socket")
                finally:
                    for response in responses:
                        response.close()
                    connection.close()
                    reader.close()
                    writer.close()

    def test_registry_cancelled_stream_closes_connection_and_partial_file(self):
        client = RegistryClient("example.test/p/app:1")

        class Response(io.BytesIO):
            status = 200

            def getheaders(self):
                return []

        class Connection:
            def __init__(self):
                self.closed = False

            def request(self, *_args, **_kwargs):
                pass

            def getresponse(self):
                return Response(b"abcdefgh")

            def close(self):
                self.closed = True

        connection = Connection()
        client.progress = lambda *_args: client.cancel()
        with tempfile.TemporaryDirectory() as temporary:
            partial = Path(temporary) / "partial"
            with patch.object(registry.http.client, "HTTPSConnection", return_value=connection), \
                    patch.object(registry, "BUFFER", 4):
                with self.assertRaises(CancelledError):
                    client.request("GET", client._url("/blobs/test"), client._scope("pull"), sink=partial)
            self.assertTrue(connection.closed)
            self.assertEqual(partial.read_bytes(), b"abcd")
            partial.unlink()  # Windows 上验证写入流也已释放。

    def test_artifactory_failed_transfer_does_not_admit_all_waiting_workers(self):
        release = threading.Event()
        gate = threading.Barrier(6)
        requests = []
        first_calls = set()
        lock = threading.Lock()
        pool_type = ThreadPoolExecutor

        class Pool:
            def __init__(self, **kwargs):
                self.pool = pool_type(**kwargs)

            def __enter__(self):
                return self.pool.__enter__()

            def __exit__(self, *args):
                release.set()
                return self.pool.__exit__(*args)

        class Response(io.BytesIO):
            headers = {"Content-Length": "1"}

            def getcode(self):
                return 200

        def request(_opener, url, **kwargs):
            name = urlsplit(url).path.rsplit("/", 1)[-1]
            with lock:
                requests.append(name)
                first = name not in first_calls
                first_calls.add(name)
            if first and int(name) < 6:
                gate.wait(5)
            if name == "0":
                raise OSError("test transfer failed")
            if not release.wait(5):
                raise AssertionError("pool did not start shutdown")
            return Response(b"x")

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "image.tar"
            files = [str(index) for index in range(12)]
            with patch.object(downloader, "ThreadPoolExecutor", Pool), \
                    patch.object(downloader, "probe_file_sizes", return_value={name: 1 for name in files}), \
                    patch.object(downloader, "request", side_effect=request), \
                    patch.object(downloader.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(OSError, "test transfer failed"):
                    downloader.download_and_pack(object(), "https://example.test/", files, output,
                                                 0, "example.test/p/app:1")
            self.assertFalse(output.exists())
        # 失败任务释放闸门到主线程收到失败之间，最多有一个等待者取得空出的名额。
        self.assertLessEqual(len(set(requests)), 7)

    def test_pack_background_failure_prevents_archive_publication(self):
        arrived = threading.Event()
        error = BrokenPipeError("test pack output failed")
        main_thread = threading.current_thread()
        read = downloader.CountingReader.read
        thread_type = threading.Thread
        escaped = []

        class Display:
            is_tty = True

            def status(self, _message):
                if threading.current_thread() is not main_thread:
                    arrived.set()
                    raise error

            def finish_line(self):
                pass

        def delayed_read(reader, *args):
            if not arrived.wait(5):
                raise AssertionError("pack heartbeat was not emitted")
            return read(reader, *args)

        def thread_factory(target, **kwargs):
            def guarded():
                try:
                    target()
                except BaseException as exc:
                    escaped.append(exc)
            return thread_type(target=guarded, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "inputs"
            inputs.mkdir()
            (inputs / "payload").write_bytes(b"x")
            output = root / "image.tar"
            with patch.object(downloader.CountingReader, "read", delayed_read), \
                    patch.object(downloader.threading, "Thread", side_effect=thread_factory):
                with self.assertRaises(BuildError) as raised:
                    downloader.pack_files(inputs, output, Display())
            self.assertIs(raised.exception.__cause__, error)
            self.assertEqual(escaped, [])
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
