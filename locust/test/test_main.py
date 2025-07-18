from __future__ import annotations

import json
import os
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from subprocess import PIPE, STDOUT
from tempfile import TemporaryDirectory
from unittest import TestCase

import gevent
import psutil
import requests
from pyquery import PyQuery as pq

from .mock_locustfile import MOCK_LOCUSTFILE_CONTENT, mock_locustfile
from .util import get_free_tcp_port, patch_env, temporary_file

SHORT_SLEEP = 2 if sys.platform == "darwin" else 1  # macOS is slow on GH, give it some extra time


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


MOCK_LOCUSTFILE_CONTENT_A = textwrap.dedent(
    """
    from locust import User, task, constant, events
    class TestUser1(User):
        wait_time = constant(1)
        @task
        def my_task(self):
            print("running my_task()")
"""
)
MOCK_LOCUSTFILE_CONTENT_B = textwrap.dedent(
    """
    from locust import User, task, constant, events
    class TestUser2(User):
        wait_time = constant(1)
        @task
        def my_task(self):
            print("running my_task()")
"""
)


class ProcessIntegrationTest(TestCase):
    def setUp(self):
        super().setUp()
        self.timeout = gevent.Timeout(10)
        self.timeout.start()

    def tearDown(self):
        self.timeout.cancel()
        super().tearDown()

    def assert_run(self, cmd: list[str], timeout: int = 5) -> subprocess.CompletedProcess[str]:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        self.assertEqual(0, out.returncode, f"locust run failed with exit code {out.returncode}:\n{out.stderr}")
        return out


class StandaloneIntegrationTests(ProcessIntegrationTest):
    def test_help_arg(self):
        output = subprocess.check_output(
            ["locust", "--help"],
            stderr=subprocess.STDOUT,
            timeout=5,
            text=True,
        ).strip()
        self.assertTrue(output.startswith("Usage: locust [options] [UserClass"))
        self.assertIn("Common options:", output)
        self.assertIn("--locustfile <filename>", output)
        self.assertIn("Logging options:", output)
        self.assertIn("--skip-log-setup      Disable Locust's logging setup.", output)

    @unittest.skipIf(os.name == "nt", reason="Signal handling on windows is hard")
    def test_custom_arguments(self):
        port = get_free_tcp_port()
        with temporary_file(
            content=textwrap.dedent(
                """
            from locust import User, task, constant, events
            @events.init_command_line_parser.add_listener
            def _(parser, **kw):
                parser.add_argument("--custom-string-arg")

            class TestUser(User):
                wait_time = constant(10)
                @task
                def my_task(self):
                    print(self.environment.parsed_options.custom_string_arg)
        """
            )
        ) as file_path:
            # print(subprocess.check_output(["cat", file_path]))
            proc = subprocess.Popen(
                ["locust", "-f", file_path, "--custom-string-arg", "command_line_value", "--web-port", str(port)],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            gevent.sleep(1)

        requests.post(
            f"http://127.0.0.1:{port}/swarm",
            data={"user_count": 1, "spawn_rate": 1, "host": "https://localhost", "custom_string_arg": "web_form_value"},
        )
        gevent.sleep(1)

        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=3)
        self.assertIn("Starting Locust", stderr)
        self.assertRegex(stderr, r".*Shutting down[\S\s]*Aggregated.*", "no stats table printed after shutting down")
        self.assertNotRegex(stderr, r".*Aggregated[\S\s]*Shutting down.*", "stats table printed BEFORE shutting down")
        self.assertNotIn("command_line_value", stdout)
        self.assertIn("web_form_value", stdout)

    @unittest.skipIf(os.name == "nt", reason="Signal handling on windows is hard")
    def test_custom_arguments_in_file(self):
        with temporary_file(
            content=textwrap.dedent(
                """
            from locust import User, task, constant, events
            @events.init_command_line_parser.add_listener
            def _(parser, **kw):
                parser.add_argument("--custom-string-arg")

            class TestUser(User):
                wait_time = constant(10)
                @task
                def my_task(self):
                    print(self.environment.parsed_options.custom_string_arg)
        """
            )
        ) as file_path:
            try:
                with open("locust.conf", "w") as conf_file:
                    conf_file.write("custom-string-arg config_file_value")
                proc = subprocess.Popen(
                    ["locust", "-f", file_path, "--autostart"],
                    stdout=PIPE,
                    stderr=PIPE,
                    text=True,
                )
                gevent.sleep(1)
            finally:
                os.remove("locust.conf")

        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=3)
        self.assertIn("Starting Locust", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertIn("config_file_value", stdout)

    @unittest.skipIf(os.name == "nt", reason="Signal handling on windows is hard")
    def test_custom_exit_code(self):
        with temporary_file(
            content=textwrap.dedent(
                """
            from locust import User, task, constant, events
            @events.quitting.add_listener
            def _(environment, **kw):
                environment.process_exit_code = 42
            @events.quit.add_listener
            def _(exit_code, **kw):
                print(f"Exit code in quit event {exit_code}")
            class TestUser(User):
                wait_time = constant(3)
                @task
                def my_task(self):
                    print("running my_task()")
        """
            )
        ) as file_path:
            proc = subprocess.Popen(["locust", "-f", file_path], stdout=PIPE, stderr=PIPE, text=True)
            gevent.sleep(1)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate()
            self.assertIn("Starting web interface at", stderr)
            self.assertIn("Starting Locust", stderr)
            self.assertIn("Shutting down (exit code 42)", stderr)
            self.assertIn("Exit code in quit event 42", stdout)
            self.assertEqual(42, proc.returncode)

    @unittest.skipIf(os.name == "nt", reason="Signal handling on windows is hard")
    def test_webserver(self):
        with temporary_file(
            content=textwrap.dedent(
                """
            from locust import User, task, constant, events
            class TestUser(User):
                wait_time = constant(3)
                @task
                def my_task(self):
                    print("running my_task()")
        """
            )
        ) as file_path:
            proc = subprocess.Popen(["locust", "-f", file_path], stdout=PIPE, stderr=PIPE, text=True)
            gevent.sleep(SHORT_SLEEP)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate()
            self.assertIn("Starting web interface at", stderr)
            self.assertIn("Starting Locust", stderr)
            self.assertIn("Shutting down (exit code 0)", stderr)

    def test_percentile_parameter(self):
        port = get_free_tcp_port()
        with temporary_file(
            content=textwrap.dedent(
                """
            from locust import User, task, constant, events
            from locust import stats
            stats.PERCENTILES_TO_CHART = [0.9, 0.4]
            class TestUser(User):
                wait_time = constant(3)
                @task
                def my_task(self):
                    print("running my_task()")
        """
            )
        ) as file_path:
            proc = subprocess.Popen(
                ["locust", "-f", file_path, "--web-port", str(port), "--autostart"], stdout=PIPE, stderr=PIPE, text=True
            )
            gevent.sleep(1)
            response = requests.get(f"http://localhost:{port}/")
            self.assertEqual(200, response.status_code)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate()
            self.assertIn("Starting web interface at", stderr)

    def test_percentiles_to_statistics(self):
        port = get_free_tcp_port()
        with temporary_file(
            content=textwrap.dedent(
                """
                from locust import User, task, constant, events
                from locust.stats import PERCENTILES_TO_STATISTICS
                PERCENTILES_TO_STATISTICS = [0.9, 0.99]
                class TestUser(User):
                    wait_time = constant(3)
                    @task
                    def my_task(self):
                        print("running my_task()")
            """
            )
        ) as file_path:
            proc = subprocess.Popen(
                ["locust", "-f", file_path, "--web-port", str(port), "--autostart"],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            gevent.sleep(1)
            response = requests.get(f"http://localhost:{port}/")
            self.assertEqual(200, response.status_code)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate()
            self.assertIn("Starting web interface at", stderr)

    def test_invalid_percentile_parameter(self):
        with temporary_file(
            content=textwrap.dedent(
                """
            from locust import User, task, constant, events
            from locust import stats
            stats.PERCENTILES_TO_CHART  = [1.2]
            class TestUser(User):
                wait_time = constant(3)
                @task
                def my_task(self):
                    print("running my_task()")
        """
            )
        ) as file_path:
            proc = subprocess.Popen(["locust", "-f", file_path, "--autostart"], stdout=PIPE, stderr=PIPE, text=True)
            stdout, stderr = proc.communicate()
            self.assertIn("parameter need to be float and value between. 0 < percentile < 1 Eg 0.95", stderr)
            self.assertEqual(1, proc.returncode)

    @unittest.skipIf(os.name == "nt", reason="Signal handling on windows is hard")
    def test_webserver_multiple_locustfiles(self):
        with mock_locustfile(content=MOCK_LOCUSTFILE_CONTENT_A) as mocked1:
            with mock_locustfile(content=MOCK_LOCUSTFILE_CONTENT_B) as mocked2:
                proc = subprocess.Popen(
                    ["locust", "-f", f"{mocked1.file_path},{mocked2.file_path}"], stdout=PIPE, stderr=PIPE, text=True
                )
                gevent.sleep(SHORT_SLEEP)
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate()
                self.assertIn("Starting web interface at", stderr)
                self.assertIn("Starting Locust", stderr)
                self.assertIn("Shutting down (exit code 0)", stderr)

    @unittest.skipIf(os.name == "nt", reason="Signal handling on windows is hard")
    def test_webserver_multiple_locustfiles_in_directory(self):
        with TemporaryDirectory() as temp_dir:
            with mock_locustfile(content=MOCK_LOCUSTFILE_CONTENT_A, dir=temp_dir):
                with mock_locustfile(content=MOCK_LOCUSTFILE_CONTENT_B, dir=temp_dir):
                    proc = subprocess.Popen(["locust", "-f", temp_dir], stdout=PIPE, stderr=PIPE, text=True)
                    gevent.sleep(SHORT_SLEEP)
                    proc.send_signal(signal.SIGTERM)
                    stdout, stderr = proc.communicate()
                    self.assertIn("Starting web interface at", stderr)
                    self.assertIn("Starting Locust", stderr)
                    self.assertIn("Shutting down (exit code 0)", stderr)

    @unittest.skipIf(os.name == "nt", reason="Signal handling on windows is hard")
    def test_webserver_multiple_locustfiles_with_shape(self):
        content = textwrap.dedent(
            """
            from locust import User, task, between
            class TestUser2(User):
                wait_time = between(2, 4)
                @task
                def my_task(self):
                    print("running my_task() again")
            """
        )
        with mock_locustfile(content=content) as mocked1:
            with temporary_file(
                content=textwrap.dedent(
                    """
                from locust import User, task, between, LoadTestShape
                class LoadTestShape(LoadTestShape):
                    def tick(self):
                        run_time = self.get_run_time()
                        if run_time < 2:
                            return (10, 1)

                        return None

                class TestUser(User):
                    wait_time = between(2, 4)
                    @task
                    def my_task(self):
                        print("running my_task()")
            """
                )
            ) as mocked2:
                proc = subprocess.Popen(
                    ["locust", "-f", f"{mocked1.file_path},{mocked2}", "-L", "DEBUG"],
                    stdout=PIPE,
                    stderr=PIPE,
                    text=True,
                )
                gevent.sleep(SHORT_SLEEP)
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate()
                self.assertIn("Starting web interface at", stderr)
                # This test is the reason we need the -L DEBUG option
                self.assertNotIn("Locust is running with the UserClass Picker Enabled", stderr)
                self.assertIn("Starting Locust", stderr)
                self.assertIn("Shutting down (exit code 0)", stderr)

    def test_default_headless_spawn_options(self):
        with mock_locustfile() as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--host",
                    "https://test.com/",
                    "--run-time",
                    "1s",
                    "--headless",
                    "--loglevel",
                    "DEBUG",
                    "--exit-code-on-error",
                    "0",
                    # just to test --stop-timeout argument parsing, doesnt actually validate its function:
                    "--stop-timeout",
                    "1s",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate(timeout=4)
            self.assertNotIn("Traceback", stderr)
            self.assertIn('Spawning additional {"UserSubclass": 1} ({"UserSubclass": 0} already running)...', stderr)
            self.assertEqual(0, proc.returncode)

    def test_invalid_stop_timeout_string(self):
        with mock_locustfile() as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--host",
                    "https://test.com/",
                    "--stop-timeout",
                    "asdf1",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate()
            self.assertIn(
                "locust: error: argument -s/--stop-timeout: Invalid time span format. Valid formats: 20, 20s, 3m, 2h, 1h20m, 3h30m10s, etc.",
                stderr,
            )
            self.assertEqual(2, proc.returncode)

    @unittest.skipIf(os.name == "nt", reason="Signal handling on windows is hard")
    def test_headless_spawn_options_wo_run_time(self):
        with mock_locustfile() as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--host",
                    "https://test.com/",
                    "--headless",
                    "--exit-code-on-error",
                    "0",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            gevent.sleep(1)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate()
            self.assertIn("Starting Locust", stderr)
            self.assertIn("No run time limit set, use CTRL+C to interrupt", stderr)
            self.assertIn("Shutting down (exit code 0)", stderr)

    @unittest.skipIf(os.name == "nt", reason="Signal handling on windows is hard")
    def test_run_headless_with_multiple_locustfiles(self):
        with TemporaryDirectory() as temp_dir:
            with mock_locustfile(dir=temp_dir):
                with temporary_file(
                    content=textwrap.dedent(
                        """
                    from locust import User, task, constant, events
                    class TestUser(User):
                        wait_time = constant(1)
                        @task
                        def my_task(self):
                            print("running my_task()")
                """
                    ),
                    dir=temp_dir,
                ):
                    proc = subprocess.Popen(
                        [
                            "locust",
                            "-f",
                            temp_dir,
                            "--headless",
                            "-u",
                            "2",
                            "--exit-code-on-error",
                            "0",
                        ],
                        stdout=PIPE,
                        stderr=PIPE,
                        text=True,
                    )
                    gevent.sleep(3)
                    proc.send_signal(signal.SIGTERM)
                    stdout, stderr = proc.communicate()
                    self.assertIn("Starting Locust", stderr)
                    self.assertIn("All users spawned:", stderr)
                    self.assertIn('"TestUser": 1', stderr)
                    self.assertIn('"UserSubclass": 1', stderr)
                    self.assertIn("Shutting down (exit code 0)", stderr)

    def test_default_headless_spawn_options_with_shape(self):
        content = MOCK_LOCUSTFILE_CONTENT + textwrap.dedent(
            """
            class LoadTestShape(LoadTestShape):
                def tick(self):
                    run_time = self.get_run_time()
                    if run_time < 2:
                        return (10, 1)

                    return None
            """
        )
        with mock_locustfile(content=content) as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--host",
                    "https://test.com/",
                    "--headless",
                    "--exit-code-on-error",
                    "0",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )

            try:
                success = True
                _, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                success = False
                proc.send_signal(signal.SIGTERM)
                _, stderr = proc.communicate()

            proc.send_signal(signal.SIGTERM)
            _, stderr = proc.communicate()
            self.assertIn("Shape test updating to 10 users at 1.00 spawn rate", stderr)
            self.assertTrue(success, "Got timeout and had to kill the process")
            # ensure stats printer printed at least one report before shutting down and that there was a final report printed as well
            self.assertRegex(stderr, r".*Aggregated[\S\s]*Shutting down[\S\s]*Aggregated.*")
            self.assertIn("Shutting down (exit code 0)", stderr)

    def test_run_headless_with_multiple_locustfiles_with_shape(self):
        content = textwrap.dedent(
            """
            from locust import User, task, between
            class TestUser2(User):
                wait_time = between(2, 4)
                @task
                def my_task(self):
                    print("running my_task() again")
            """
        )
        with mock_locustfile(content=content) as mocked1:
            with temporary_file(
                content=textwrap.dedent(
                    """
                from locust import User, task, between, LoadTestShape
                class LoadTestShape(LoadTestShape):
                    def tick(self):
                        run_time = self.get_run_time()
                        if run_time < 2:
                            return (10, 1)

                        return None

                class TestUser(User):
                    wait_time = between(2, 4)
                    @task
                    def my_task(self):
                        print("running my_task()")
            """
                )
            ) as mocked2:
                proc = subprocess.Popen(
                    [
                        "locust",
                        "-f",
                        f"{mocked1.file_path},{mocked2}",
                        "--host",
                        "https://test.com/",
                        "--headless",
                        "--exit-code-on-error",
                        "0",
                    ],
                    stdout=PIPE,
                    stderr=PIPE,
                    text=True,
                )

                try:
                    success = True
                    _, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    success = False

                proc.send_signal(signal.SIGTERM)
                _, stderr = proc.communicate()
                self.assertIn("Shape test updating to 10 users at 1.00 spawn rate", stderr)
                self.assertTrue(success, "Got timeout and had to kill the process")
                # ensure stats printer printed at least one report before shutting down and that there was a final report printed as well
                self.assertRegex(stderr, r".*Aggregated[\S\s]*Shutting down[\S\s]*Aggregated.*")
                self.assertIn("Shutting down (exit code 0)", stderr)

    @unittest.skipIf(os.name == "nt", reason="Signal handling on windows is hard")
    def test_autostart_wo_run_time(self):
        port = get_free_tcp_port()
        with mock_locustfile() as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--web-port",
                    str(port),
                    "--autostart",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            gevent.sleep(1.9)
            try:
                response = requests.get(f"http://localhost:{port}/")
            except Exception:
                response = None
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate()
            self.assertIn("Starting Locust", stderr)
            self.assertIn("No run time limit set, use CTRL+C to interrupt", stderr)
            self.assertIn("Shutting down ", stderr)
            self.assertNotIn("Traceback", stderr)
            # check response afterwards, because it really isn't as informative as stderr
            self.assertEqual(200, response.status_code)
            d = pq(response.content.decode("utf-8"))

            self.assertEqual(200, response.status_code)
            self.assertIn('"state": "running"', str(d))

    @unittest.skipIf(sys.platform == "darwin", reason="This is too messy on macOS")
    def test_autostart_w_run_time(self):
        port = get_free_tcp_port()
        with mock_locustfile() as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--web-port",
                    str(port),
                    "-t",
                    "3",
                    "--autostart",
                    "--autoquit",
                    "1",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            gevent.sleep(2.8)
            try:
                response = requests.get(f"http://localhost:{port}/")
            except Exception:
                response = None
            _, stderr = proc.communicate(timeout=4)
            self.assertIn("Starting Locust", stderr)
            self.assertIn("Run time limit set to 3 seconds", stderr)
            self.assertIn("Shutting down ", stderr)
            self.assertNotIn("Traceback", stderr)
            # check response afterwards, because it really isn't as informative as stderr
            d = pq(response.content.decode("utf-8"))

            self.assertEqual(200, response.status_code)
            self.assertIn('"state": "running"', str(d))

    @unittest.skipIf(os.name == "nt", reason="Signal handling on windows is hard")
    def test_run_autostart_with_multiple_locustfiles(self):
        with TemporaryDirectory() as temp_dir:
            with mock_locustfile(dir=temp_dir):
                with temporary_file(
                    content=textwrap.dedent(
                        """
                    from locust import User, task, constant, events
                    class TestUser(User):
                        wait_time = constant(1)
                        @task
                        def my_task(self):
                            print("running my_task()")
                """
                    ),
                    dir=temp_dir,
                ):
                    proc = subprocess.Popen(
                        [
                            "locust",
                            "-f",
                            temp_dir,
                            "--autostart",
                            "-u",
                            "2",
                            "--exit-code-on-error",
                            "0",
                        ],
                        stdout=PIPE,
                        stderr=PIPE,
                        text=True,
                    )
                    gevent.sleep(3)
                    proc.send_signal(signal.SIGTERM)
                    stdout, stderr = proc.communicate()
                    self.assertIn("Starting Locust", stderr)
                    self.assertIn("All users spawned:", stderr)
                    self.assertIn('"TestUser": 1', stderr)
                    self.assertIn('"UserSubclass": 1', stderr)
                    self.assertIn("Shutting down (exit code 0)", stderr)

    def test_autostart_w_load_shape(self):
        port = get_free_tcp_port()
        with mock_locustfile(
            content=MOCK_LOCUSTFILE_CONTENT
            + textwrap.dedent(
                """
            from locust import LoadTestShape
            class LoadTestShape(LoadTestShape):
                def tick(self):
                    run_time = self.get_run_time()
                    if run_time < 2:
                        return (10, 1)

                    return None
            """
            )
        ) as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--web-port",
                    str(port),
                    "--autostart",
                    "--autoquit",
                    "3",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            gevent.sleep(2.8)
            response = requests.get(f"http://localhost:{port}/")
            try:
                success = True
                _, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                success = False
                proc.send_signal(signal.SIGTERM)
                _, stderr = proc.communicate()

            self.assertIn("Starting Locust", stderr)
            self.assertIn("Shape test starting", stderr)
            self.assertIn("Shutting down ", stderr)
            self.assertIn("autoquit time reached", stderr)
            # check response afterwards, because it really isn't as informative as stderr
            self.assertEqual(200, response.status_code)
            self.assertTrue(success, "got timeout and had to kill the process")

    def test_autostart_multiple_locustfiles_with_shape(self):
        port = get_free_tcp_port()
        content = textwrap.dedent(
            """
            from locust import User, task, between
            class TestUser2(User):
                wait_time = between(2, 4)
                @task
                def my_task(self):
                    print("running my_task() again")
            """
        )
        with mock_locustfile(content=content) as mocked1:
            with temporary_file(
                content=textwrap.dedent(
                    """
                from locust import User, task, between, LoadTestShape
                class LoadTestShape(LoadTestShape):
                    def tick(self):
                        run_time = self.get_run_time()
                        if run_time < 2:
                            return (10, 1)

                        return None

                class TestUser(User):
                    wait_time = between(2, 4)
                    @task
                    def my_task(self):
                        print("running my_task()")
            """
                )
            ) as mocked2:
                proc = subprocess.Popen(
                    [
                        "locust",
                        "-f",
                        f"{mocked1.file_path},{mocked2}",
                        "--web-port",
                        str(port),
                        "--autostart",
                        "--autoquit",
                        "3",
                    ],
                    stdout=PIPE,
                    stderr=PIPE,
                    text=True,
                )
                gevent.sleep(2.8)
                success = True
                try:
                    response = requests.get(f"http://localhost:{port}/")
                except ConnectionError:
                    success = False
                    response = None
                try:
                    _, stderr = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    success = False
                    proc.send_signal(signal.SIGTERM)
                    _, stderr = proc.communicate()

                self.assertIn("Starting Locust", stderr)
                self.assertIn("Shape test starting", stderr)
                self.assertIn("Shutting down ", stderr)
                self.assertIn("autoquit time reached", stderr)
                # check response afterwards, because it really isn't as informative as stderr
                self.assertEqual(200, response.status_code)
                self.assertTrue(success, "got timeout and had to kill the process")

    @unittest.skipIf(platform.system() == "Darwin", reason="Messy on macOS on GH")
    @unittest.skipIf(os.name == "nt", reason="Signal handling on windows is hard")
    def test_web_options(self):
        port = get_free_tcp_port()
        if platform.system() != "Darwin":
            # MacOS only sets up the loopback interface for 127.0.0.1 and not for 127.*.*.*, so we can't test this
            with mock_locustfile() as mocked:
                proc = subprocess.Popen(
                    ["locust", "-f", mocked.file_path, "--web-host", "127.0.0.2", "--web-port", str(port)],
                    stdout=PIPE,
                    stderr=PIPE,
                )
                gevent.sleep(1)
                self.assertEqual(200, requests.get(f"http://127.0.0.2:{port}/", timeout=1).status_code)
                proc.terminate()

        with mock_locustfile() as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--web-host",
                    "*",
                    "--web-port",
                    str(port),
                ],
                stdout=PIPE,
                stderr=PIPE,
            )
            gevent.sleep(1)
            self.assertEqual(200, requests.get(f"http://127.0.0.1:{port}/", timeout=3).status_code)
            proc.terminate()

    @unittest.skipIf(os.name == "nt", reason="termios doesnt exist on windows, and thus we cannot import pty")
    def test_input(self):
        import pty

        LOCUSTFILE_CONTENT = textwrap.dedent(
            """
        from locust import User, TaskSet, task, between

        class UserSubclass(User):
            wait_time = between(0.2, 0.8)
            @task
            def t(self):
                print("Test task is running")
        """
        )
        with mock_locustfile(content=LOCUSTFILE_CONTENT) as mocked:
            stdin_m, stdin_s = pty.openpty()
            stdin = os.fdopen(stdin_m, "wb", 0)

            proc = subprocess.Popen(
                " ".join(
                    [
                        "locust",
                        "-f",
                        mocked.file_path,
                        "--headless",
                        "--run-time",
                        "6s",
                        "-u",
                        "0",
                        "--loglevel",
                        "INFO",
                    ]
                ),
                stderr=STDOUT,
                stdin=stdin_s,
                stdout=PIPE,
                shell=True,
                text=True,
            )
            gevent.sleep(1)

            stdin.write(b"w")
            gevent.sleep(1)
            stdin.write(b"W")
            gevent.sleep(1)
            stdin.write(b"s")
            gevent.sleep(1)
            stdin.write(b"S")
            gevent.sleep(1)

            # This should not do anything since we are already at zero users
            stdin.write(b"S")

            output = proc.communicate()[0]
            stdin.close()
            self.assertIn("Ramping to 1 users at a rate of 100.00 per second", output)
            self.assertIn('All users spawned: {"UserSubclass": 1} (1 total users)', output)
            self.assertIn("Ramping to 11 users at a rate of 100.00 per second", output)
            self.assertIn('All users spawned: {"UserSubclass": 11} (11 total users)', output)
            self.assertIn("Ramping to 10 users at a rate of 100.00 per second", output)
            self.assertIn('All users spawned: {"UserSubclass": 10} (10 total users)', output)
            self.assertIn("Ramping to 0 users at a rate of 100.00 per second", output)
            self.assertIn('All users spawned: {"UserSubclass": 0} (0 total users)', output)
            self.assertIn("Test task is running", output)
            # ensure stats printer printed at least one report before shutting down and that there was a final report printed as well
            self.assertRegex(output, r".*Aggregated[\S\s]*Shutting down[\S\s]*Aggregated.*")
            self.assertIn("Shutting down (exit code 0)", output)

    @unittest.skipIf(os.name == "nt", reason="termios doesnt exist on windows, and thus we cannot import pty")
    def test_autospawn_browser(self):
        import pty

        LOCUSTFILE_CONTENT = textwrap.dedent(
            """

        from pytest import MonkeyPatch
        import sys
        import webbrowser

        monkeypatch = MonkeyPatch()

        def open_new_tab(url):
            print("browser opened with url", url)
            sys.exit(0)

        monkeypatch.setattr(webbrowser, "open_new_tab", open_new_tab)
        print("patched")
        from locust import User, TaskSet, task, between
        class UserSubclass(User):
            @task
            def t(self):
                print("Test task is running")

        """
        )
        with mock_locustfile(content=LOCUSTFILE_CONTENT) as mocked:
            stdin_m, stdin_s = pty.openpty()
            stdin = os.fdopen(stdin_m, "wb", 0)

            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                ],
                stdin=stdin_s,
                stdout=PIPE,
                text=True,
            )
            gevent.sleep(1)
            stdin.write(b"\n")
            try:
                output, _ = proc.communicate(timeout=1)
            except Exception:
                proc.kill()
                output, _ = proc.communicate()

            self.assertIn("browser opened", output)

    def test_spawning_with_fixed(self):
        LOCUSTFILE_CONTENT = textwrap.dedent(
            """
        from locust import User, task, constant

        class User1(User):
            fixed_count = 2
            wait_time = constant(1)

            @task
            def t(self):
                print("Test task is running")

        class User2(User):
            wait_time = constant(1)
            @task
            def t(self):
                print("Test task is running")

        class User3(User):
            wait_time = constant(1)
            @task
            def t(self):
                print("Test task is running")
        """
        )
        with mock_locustfile(content=LOCUSTFILE_CONTENT) as mocked:
            proc = subprocess.Popen(
                " ".join(
                    [
                        "locust",
                        "-f",
                        mocked.file_path,
                        "--headless",
                        "--run-time",
                        "5s",
                        "-u",
                        "10",
                        "-r",
                        "10",
                        "--loglevel",
                        "INFO",
                    ]
                ),
                stderr=STDOUT,
                stdout=PIPE,
                shell=True,
                text=True,
            )

            output = proc.communicate()[0]
            self.assertIn("Ramping to 10 users at a rate of 10.00 per second", output)
            self.assertIn('All users spawned: {"User1": 2, "User2": 4, "User3": 4} (10 total users)', output)
            self.assertIn("Test task is running", output)
            # ensure stats printer printed at least one report before shutting down and that there was a final report printed as well
            self.assertRegex(output, r".*Aggregated[\S\s]*Shutting down[\S\s]*Aggregated.*")
            self.assertIn("Shutting down (exit code 0)", output)

    def test_spawing_with_fixed_multiple_locustfiles(self):
        with mock_locustfile(content=MOCK_LOCUSTFILE_CONTENT_A) as mocked1:
            with mock_locustfile(content=MOCK_LOCUSTFILE_CONTENT_B) as mocked2:
                proc = subprocess.Popen(
                    " ".join(
                        [
                            "locust",
                            "-f",
                            f"{mocked1.file_path},{mocked2.file_path}",
                            "--headless",
                            "--run-time",
                            "2s",
                            "-u",
                            "10",
                            "-r",
                            "10",
                            "--loglevel",
                            "INFO",
                        ]
                    ),
                    stderr=STDOUT,
                    stdout=PIPE,
                    shell=True,
                    text=True,
                )

                output = proc.communicate()[0]
                self.assertIn("Ramping to 10 users at a rate of 10.00 per second", output)
                self.assertIn('All users spawned: {"TestUser1": 5, "TestUser2": 5} (10 total users)', output)
                self.assertIn("running my_task()", output)
                # ensure stats printer printed at least one report before shutting down and that there was a final report printed as well
                self.assertRegex(output, r".*Aggregated[\S\s]*Shutting down[\S\s]*Aggregated.*")
                self.assertIn("Shutting down (exit code 0)", output)

    def test_warning_with_lower_user_count_than_fixed_count(self):
        LOCUSTFILE_CONTENT = textwrap.dedent(
            """
        from locust import User, task, constant

        class User1(User):
            fixed_count = 2
            wait_time = constant(1)

            @task
            def t(self):
                pass

        class User2(User):
            fixed_count = 2
            wait_time = constant(1)

            @task
            def t(self):
                pass
        """
        )
        with mock_locustfile(content=LOCUSTFILE_CONTENT) as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--headless",
                    "--run-time",
                    "1",
                    "-u",
                    "3",
                ],
                stderr=STDOUT,
                stdout=PIPE,
                text=True,
            )

            output = proc.communicate()[0]
            self.assertIn("Total fixed_count of User classes (4) is greater than ", output)

    def test_with_package_as_locustfile(self):
        with TemporaryDirectory() as temp_dir:
            with open(f"{temp_dir}/__init__.py", mode="w"):
                with mock_locustfile(dir=temp_dir):
                    proc = subprocess.Popen(
                        [
                            "locust",
                            "-f",
                            temp_dir,
                            "--headless",
                            "--exit-code-on-error",
                            "0",
                            "--run-time",
                            "2",
                        ],
                        stdout=PIPE,
                        stderr=PIPE,
                        text=True,
                    )
                    stdout, stderr = proc.communicate()
                    self.assertIn("Starting Locust", stderr)
                    self.assertIn("All users spawned:", stderr)
                    self.assertIn('"UserSubclass": 1', stderr)
                    self.assertIn("Shutting down (exit code 0)", stderr)

    def test_command_line_user_selection(self):
        LOCUSTFILE_CONTENT = textwrap.dedent(
            """
        from locust import User, task, constant

        class User1(User):
            wait_time = constant(1)
            @task
            def t(self):
                print("User1 is running")

        class User2(User):
            wait_time = constant(1)
            @task
            def t(self):
                print("User2 is running")

        class User3(User):
            wait_time = constant(1)
            @task
            def t(self):
                print("User3 is running")
        """
        )
        with mock_locustfile(content=LOCUSTFILE_CONTENT) as mocked:
            proc = subprocess.Popen(
                " ".join(
                    [
                        "locust",
                        "-f",
                        mocked.file_path,
                        "--headless",
                        "--run-time",
                        "2s",
                        "-u",
                        "5",
                        "-r",
                        "10",
                        "User2",
                        "User3",
                    ]
                ),
                stderr=STDOUT,
                stdout=PIPE,
                shell=True,
                text=True,
            )

            output = proc.communicate()[0]
            self.assertNotIn("User1 is running", output)
            self.assertIn("User2 is running", output)
            self.assertIn("User3 is running", output)
            self.assertEqual(0, proc.returncode)

    def test_html_report_option(self):
        html_template = "some_name_{u}_{r}_{t}.html"
        expected_filename = "some_name_11_5_2.html"

        with mock_locustfile() as mocked:
            # Get system temp directory
            temp_dir = tempfile.gettempdir()

            # Define the input filename as well as the resulting filename within the temp directory
            html_report_file_path = os.path.join(temp_dir, html_template)
            output_html_report_file_path = os.path.join(temp_dir, expected_filename)

            try:
                output = subprocess.check_output(
                    [
                        "locust",
                        "-f",
                        mocked.file_path,
                        "--host",
                        "https://test.com/",
                        "--run-time",
                        "2s",
                        "--headless",
                        "--exit-code-on-error",
                        "0",
                        "-u",
                        "11",
                        "-r",
                        "5",
                        "--html",
                        html_report_file_path,
                    ],
                    stderr=subprocess.STDOUT,
                    timeout=10,
                    text=True,
                ).strip()

            except subprocess.CalledProcessError as e:
                raise AssertionError(f"Running locust command failed. Output was:\n\n{e.stdout}") from e
            with open(output_html_report_file_path, encoding="utf-8") as f:
                html_report_content = f.read()

            # make sure correct name is generated based on filename arguments
            self.assertIn(expected_filename, output)

            _, locustfile = os.path.split(mocked.file_path)
            self.assertIn(locustfile, html_report_content)

            # make sure host appears in the report
            self.assertIn("https://test.com/", html_report_content)
            self.assertIn('"show_download_link": false', html_report_content)
            self.assertRegex(html_report_content, r'"start_time": "\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"')
            self.assertRegex(html_report_content, r'"end_time": "\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"')
            self.assertRegex(html_report_content, r'"duration": "\d* seconds?"')

    def test_run_with_userclass_picker(self):
        with temporary_file(content=MOCK_LOCUSTFILE_CONTENT_A) as file1:
            with temporary_file(content=MOCK_LOCUSTFILE_CONTENT_B) as file2:
                proc = subprocess.Popen(
                    ["locust", "-f", f"{file1},{file2}", "--class-picker", "-L", "DEBUG"],
                    stdout=PIPE,
                    stderr=PIPE,
                    text=True,
                )
                gevent.sleep(2)
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate()

                self.assertIn("Locust is running with the UserClass Picker Enabled", stderr)
                self.assertIn("Starting Locust", stderr)
                self.assertIn("Starting web interface at", stderr)

    def test_error_when_duplicate_userclass_names(self):
        MOCK_LOCUSTFILE_CONTENT_C = textwrap.dedent(
            """
            from locust import User, task, constant, events
            class TestUser1(User):
                wait_time = constant(3)
                @task
                def my_task(self):
                    print("running my_task()")
        """
        )
        with temporary_file(content=MOCK_LOCUSTFILE_CONTENT_A) as file1:
            with temporary_file(content=MOCK_LOCUSTFILE_CONTENT_C) as file2:
                proc = subprocess.Popen(["locust", "-f", f"{file1},{file2}"], stdout=PIPE, stderr=PIPE, text=True)
                stdout, stderr = proc.communicate()

                self.assertIn("Duplicate user class names: TestUser1 is defined", stderr)
                self.assertEqual(1, proc.returncode)

    def test_no_error_when_same_userclass_in_two_files(self):
        with temporary_file(content=MOCK_LOCUSTFILE_CONTENT_A) as file1:
            MOCK_LOCUSTFILE_CONTENT_C = textwrap.dedent(
                f"""
                from {os.path.basename(file1)[:-3]} import TestUser1
            """
            )
            print(MOCK_LOCUSTFILE_CONTENT_C)
            with temporary_file(content=MOCK_LOCUSTFILE_CONTENT_C) as file2:
                proc = subprocess.Popen(
                    ["locust", "-f", f"{file1},{file2}", "-t", "1", "--headless"], stdout=PIPE, stderr=PIPE, text=True
                )
                stdout, stderr = proc.communicate()

                self.assertIn("running my_task", stdout)
                self.assertEqual(0, proc.returncode)

    def test_error_when_duplicate_shape_class_names(self):
        MOCK_LOCUSTFILE_CONTENT_C = MOCK_LOCUSTFILE_CONTENT_A + textwrap.dedent(
            """
            from locust import LoadTestShape
            class TestShape(LoadTestShape):
                def tick(self):
                    run_time = self.get_run_time()
                    if run_time < 2:
                        return (10, 1)

                    return None
            """
        )
        MOCK_LOCUSTFILE_CONTENT_D = MOCK_LOCUSTFILE_CONTENT_B + textwrap.dedent(
            """
            from locust import LoadTestShape
            class TestShape(LoadTestShape):
                def tick(self):
                    run_time = self.get_run_time()
                    if run_time < 2:
                        return (10, 1)

                    return None
            """
        )
        with temporary_file(content=MOCK_LOCUSTFILE_CONTENT_C) as file1:
            with temporary_file(content=MOCK_LOCUSTFILE_CONTENT_D) as file2:
                proc = subprocess.Popen(["locust", "-f", f"{file1},{file2}"], stdout=PIPE, stderr=PIPE, text=True)
                stdout, stderr = proc.communicate()

                self.assertIn("Duplicate shape classes: TestShape", stderr)
                self.assertEqual(1, proc.returncode)

    def test_error_when_providing_both_run_time_and_a_shape_class(self):
        content = MOCK_LOCUSTFILE_CONTENT + textwrap.dedent(
            """
            from locust import LoadTestShape
            class TestShape(LoadTestShape):
                def tick(self):
                    return None
            """
        )
        with mock_locustfile(content=content) as mocked:
            out = self.assert_run(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--run-time=1s",
                    "--headless",
                    "--exit-code-on-error",
                    "0",
                ]
            )

            self.assertIn("--run-time, --users or --spawn-rate have no impact on LoadShapes", out.stderr)
            self.assertIn("The following option(s) will be ignored: --run-time", out.stderr)

    def test_shape_class_log_disabled_parameters(self):
        content = MOCK_LOCUSTFILE_CONTENT + textwrap.dedent(
            """
            from locust import LoadTestShape

            class TestShape(LoadTestShape):
                def tick(self):
                    return None
            """
        )
        with mock_locustfile(content=content) as mocked:
            out = self.assert_run(
                [
                    "locust",
                    "--headless",
                    "-f",
                    mocked.file_path,
                    "--exit-code-on-error=0",
                    "--users=1",
                    "--spawn-rate=1",
                ]
            )
            self.assertIn("Shape test starting.", out.stderr)
            self.assertIn("--run-time, --users or --spawn-rate have no impact on LoadShapes", out.stderr)
            self.assertIn("The following option(s) will be ignored: --users, --spawn-rate", out.stderr)

    def test_shape_class_with_use_common_options(self):
        content = MOCK_LOCUSTFILE_CONTENT + textwrap.dedent(
            """
            from locust import LoadTestShape

            class TestShape(LoadTestShape):
                use_common_options = True

                def tick(self):
                    return None
            """
        )
        with mock_locustfile(content=content) as mocked:
            out = self.assert_run(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--run-time=1s",
                    "--users=1",
                    "--spawn-rate=1",
                    "--headless",
                    "--exit-code-on-error=0",
                ]
            )
            self.assertIn("Shape test starting.", out.stderr)
            self.assertNotIn("--run-time, --users or --spawn-rate have no impact on LoadShapes", out.stderr)
            self.assertNotIn("The following option(s) will be ignored:", out.stderr)

    def test_error_when_locustfiles_directory_is_empty(self):
        with TemporaryDirectory() as temp_dir:
            proc = subprocess.Popen(["locust", "-f", temp_dir], stdout=PIPE, stderr=PIPE, text=True)
            stdout, stderr = proc.communicate()

            self.assertIn(f"Could not find any locustfiles in directory '{temp_dir}'", stderr)
            self.assertEqual(1, proc.returncode)

    def test_error_when_no_tasks_match_tags(self):
        content = """
from locust import HttpUser, TaskSet, task, constant, LoadTestShape, tag
class MyUser(HttpUser):
    host = "http://127.0.0.1:8089"
    wait_time = constant(1)
    @tag("tag1")
    @task
    def task1(self):
        print("task1")
    """
        with mock_locustfile(content=content) as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--headless",
                    "-t",
                    "1",
                    "--tags",
                    "tag2",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate()
            self.assertIn("MyUser had no tasks left after filtering", stderr)
            self.assertIn("No tasks defined on MyUser", stderr)
            self.assertEqual(1, proc.returncode)

    @unittest.skipIf(os.name == "nt", reason="Signal handling on windows is hard")
    def test_graceful_exit_when_keyboard_interrupt(self):
        with temporary_file(
            content=textwrap.dedent(
                """
                from locust import User, events, task, constant, LoadTestShape
                @events.test_stop.add_listener
                def on_test_stop(environment, **kwargs) -> None:
                    print("Test Stopped")

                class LoadTestShape(LoadTestShape):
                    def tick(self):
                        run_time = self.get_run_time()
                        if run_time < 2:
                            return (10, 1)

                        return None

                class TestUser(User):
                    wait_time = constant(3)
                    @task
                    def my_task(self):
                        print("running my_task()")
            """
            )
        ) as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked,
                    "--headless",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            gevent.sleep(1.9)
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate()
            print(stderr, stdout)
            self.assertIn("Shape test starting", stderr)
            self.assertIn("Exiting due to CTRL+C interruption", stderr)
            self.assertIn("Test Stopped", stdout)
            # ensure stats printer printed at least one report before shutting down and that there was a final report printed as well
            self.assertRegex(stderr, r".*Aggregated[\S\s]*Shutting down[\S\s]*Aggregated.*")

    def test_exception_in_init_event(self):
        with mock_locustfile(
            content=textwrap.dedent(
                """
            from locust import User, task, constant, events
            @events.init.add_listener
            def _(*args, **kw):
                raise Exception("something went wrong")

            class TestUser(User):
                wait_time = constant(10)
                @task
                def my_task(self):
                    pass
                    """
            )
        ) as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--host",
                    "https://test.com/",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate(timeout=3)
            self.assertIn("something went wrong", stderr)
            self.assertEqual(1, proc.returncode)


class DistributedIntegrationTests(ProcessIntegrationTest):
    failed_port_check = False

    def setUp(self):
        if self.failed_port_check:
            # fail immediately
            raise Exception("Port 5557 was (still) busy when starting a new test case")
        for _ in range(5):
            if not is_port_in_use(5557):
                break
            else:
                gevent.sleep(1)
        else:
            self.failed_port_check = True
            raise Exception("Port 5557 was (still) busy when starting a new test case")
        super().setUp()

    def test_expect_workers(self):
        with mock_locustfile() as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--headless",
                    "--master",
                    "--expect-workers",
                    "2",
                    "--expect-workers-max-wait",
                    "1",
                    "-L",
                    "DEBUG",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            _, stderr = proc.communicate()
            self.assertIn("Waiting for workers to be ready, 0 of 2 connected", stderr)
            self.assertIn("Gave up waiting for workers to connect", stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertEqual(1, proc.returncode)

    def test_distributed_events(self):
        content = (
            MOCK_LOCUSTFILE_CONTENT
            + """
from locust import events
from locust.runners import MasterRunner
@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    if isinstance(environment.runner, MasterRunner):
        print("test_start on master")
    else:
        print("test_start on worker")

@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    if isinstance(environment.runner, MasterRunner):
        print("test_stop on master")
    else:
        print("test_stop on worker")
"""
        )
        with mock_locustfile(content=content) as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--headless",
                    "--master",
                    "--expect-workers",
                    "1",
                    "-t",
                    "1",
                    "--exit-code-on-error",
                    "0",
                    "-L",
                    "DEBUG",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            proc_worker = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--worker",
                    "-L",
                    "DEBUG",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate()
            stdout_worker, stderr_worker = proc_worker.communicate()
            self.assertIn("test_start on master", stdout)
            self.assertIn("test_stop on master", stdout)
            self.assertIn("test_stop on worker", stdout_worker)
            self.assertIn("test_start on worker", stdout_worker)
            self.assertNotIn("Traceback", stderr)
            self.assertNotIn("Traceback", stderr_worker)
            self.assertEqual(0, proc.returncode)
            self.assertEqual(0, proc_worker.returncode)

    def test_distributed_tags(self):
        content = """
from locust import HttpUser, TaskSet, task, between, LoadTestShape, tag
class SecondUser(HttpUser):
    host = "http://127.0.0.1:8089"
    wait_time = between(0, 0.1)
    @tag("tag1")
    @task
    def task1(self):
        print("task1")

    @tag("tag2")
    @task
    def task2(self):
        print("task2")
"""
        with mock_locustfile(content=content) as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--headless",
                    "--master",
                    "--expect-workers",
                    "1",
                    "-t",
                    "1",
                    "-u",
                    "2",
                    "--exit-code-on-error",
                    "0",
                    "-L",
                    "DEBUG",
                    "--tags",
                    "tag1",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            proc_worker = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--worker",
                    "-L",
                    "DEBUG",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate()
            stdout_worker, stderr_worker = proc_worker.communicate()
            self.assertNotIn("ERROR", stderr_worker)
            self.assertIn("task1", stdout_worker)
            self.assertNotIn("task2", stdout_worker)
            self.assertNotIn("Traceback", stderr)
            self.assertNotIn("Traceback", stderr_worker)
            self.assertEqual(0, proc.returncode)
            self.assertEqual(0, proc_worker.returncode)

    def test_distributed(self):
        LOCUSTFILE_CONTENT = textwrap.dedent(
            """
            from locust import User, task, constant

            class User1(User):
                wait_time = constant(1)

                @task
                def t(self):
                    pass
            """
        )
        with mock_locustfile(content=LOCUSTFILE_CONTENT) as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--headless",
                    "--master",
                    "--expect-workers",
                    "1",
                    "-u",
                    "3",
                    "-r",
                    "99",
                    "-t",
                    "1s",
                ],
                stderr=STDOUT,
                stdout=PIPE,
                text=True,
            )
            proc_worker = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--worker",
                ],
                stderr=STDOUT,
                stdout=PIPE,
                text=True,
            )
            try:
                stdout = proc.communicate(timeout=9)[0]
            except Exception:
                proc.kill()
                proc_worker.kill()
                stdout = proc.communicate()[0]
                worker_stdout = proc_worker.communicate()[0]
                assert False, f"master never finished: {stdout}, worker output: {worker_stdout}"
            stdout = proc.communicate()[0]
            proc_worker.communicate()

            self.assertIn('All users spawned: {"User1": 3} (3 total users)', stdout)
            self.assertIn("Shutting down (exit code 0)", stdout)
            self.assertEqual(0, proc_worker.returncode)

    def test_distributed_report_timeout_expired(self):
        LOCUSTFILE_CONTENT = textwrap.dedent(
            """
            from locust import User, task, constant

            class User1(User):
                wait_time = constant(1)

                @task
                def t(self):
                    pass
            """
        )
        with (
            mock_locustfile(content=LOCUSTFILE_CONTENT) as mocked,
            patch_env("LOCUST_WAIT_FOR_WORKERS_REPORT_AFTER_RAMP_UP", "0.01") as _,
        ):
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--headless",
                    "--master",
                    "--expect-workers",
                    "1",
                    "-u",
                    "3",
                    "-r",
                    "99",
                    "-t",
                    "1s",
                ],
                stderr=STDOUT,
                stdout=PIPE,
                text=True,
            )
            proc_worker = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--worker",
                ],
                stderr=STDOUT,
                stdout=PIPE,
                text=True,
            )
            stdout = proc.communicate()[0]
            proc_worker.communicate()

            self.assertIn(
                "Spawning is complete and report waittime is expired, but not all reports received from workers:",
                stdout,
            )
            self.assertIn("Shutting down (exit code 0)", stdout)
            self.assertEqual(0, proc_worker.returncode)

    def test_locustfile_distribution(self):
        LOCUSTFILE_CONTENT = textwrap.dedent(
            """
            from locust import User, task, constant

            class User1(User):
                wait_time = constant(1)

                @task
                def t(self):
                    pass
            """
        )
        with mock_locustfile(content=LOCUSTFILE_CONTENT) as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--headless",
                    "--master",
                    "--expect-workers",
                    "2",
                    "-t",
                    "1s",
                ],
                stderr=STDOUT,
                stdout=PIPE,
                text=True,
            )
            proc_worker = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    "-",
                    "--worker",
                ],
                stderr=STDOUT,
                stdout=PIPE,
                text=True,
            )
            gevent.sleep(1)
            proc_worker2 = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    "-",
                    "--worker",
                ],
                stderr=STDOUT,
                stdout=PIPE,
                text=True,
            )
            stdout = proc.communicate()[0]
            stdout_worker = proc_worker.communicate()[0]
            stdout_worker2 = proc_worker2.communicate()[0]

            self.assertIn('All users spawned: {"User1": 1} (1 total users)', stdout)
            self.assertIn("Shutting down (exit code 0)", stdout)
            self.assertNotIn("Traceback", stdout)
            self.assertNotIn("Traceback", stdout_worker)
            self.assertNotIn("Traceback", stdout_worker2)
            self.assertEqual(0, proc_worker.returncode)

    def test_locustfile_distribution_with_workers_started_first(self):
        LOCUSTFILE_CONTENT = textwrap.dedent(
            """
            from locust import User, task, constant

            class User1(User):
                wait_time = constant(1)

                @task
                def t(self):
                    print("hello")
            """
        )
        with mock_locustfile(content=LOCUSTFILE_CONTENT) as mocked:
            proc_worker = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    "-",
                    "--worker",
                ],
                stderr=STDOUT,
                stdout=PIPE,
                text=True,
            )
            gevent.sleep(1)
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--headless",
                    "--master",
                    "--expect-workers",
                    "1",
                    "-t",
                    "1",
                ],
                stderr=STDOUT,
                stdout=PIPE,
                text=True,
            )

            stdout = proc.communicate()[0]
            worker_stdout = proc_worker.communicate()[0]

            self.assertIn('All users spawned: {"User1": ', stdout)
            self.assertIn("Shutting down (exit code 0)", stdout)
            self.assertEqual(0, proc_worker.returncode)
            self.assertIn("hello", worker_stdout)

    def test_distributed_with_locustfile_distribution_not_plain_filename(self):
        LOCUSTFILE_CONTENT = textwrap.dedent(
            """
            from locust import User, task, constant

            class User1(User):
                wait_time = constant(1)

                @task
                def t(self):
                    pass
            """
        )
        with mock_locustfile(content=LOCUSTFILE_CONTENT) as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    f"{mocked.file_path},{mocked.file_path}",
                    "--headless",
                    "--master",
                    "--expect-workers",
                    "1",
                    "-t",
                    "1s",
                ],
                stderr=STDOUT,
                stdout=PIPE,
                text=True,
            )
            proc_worker = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    "-",
                    "--worker",
                ],
                stderr=STDOUT,
                stdout=PIPE,
                text=True,
            )

            stdout = proc.communicate()[0]
            stdout_worker = proc_worker.communicate()[0]

            self.assertIn('All users spawned: {"User1": 1} (1 total users)', stdout)
            self.assertIn("Shutting down (exit code 0)", stdout)
            self.assertNotIn("Traceback", stdout)
            self.assertNotIn("Traceback", stdout_worker)
            self.assertEqual(0, proc_worker.returncode)

    def test_json_schema(self):
        LOCUSTFILE_CONTENT = textwrap.dedent(
            """
            from locust import HttpUser, task, constant

            class QuickstartUser(HttpUser):
                wait_time = constant(1)

                @task
                def hello_world(self):
                    self.client.get("/")

            """
        )
        with mock_locustfile(content=LOCUSTFILE_CONTENT) as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--host",
                    "http://google.com",
                    "--headless",
                    "-u",
                    "1",
                    "-t",
                    "2s",
                    "--json",
                ],
                stderr=PIPE,
                stdout=PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate()

            try:
                data = json.loads(stdout)
            except json.JSONDecodeError:
                self.fail(f"Trying to parse {stdout} as json failed")

            self.assertEqual(0, proc.returncode)

            if not data:
                self.fail(f"No data in json: {stdout}, stderr: {stderr}")

            result = data[0]
            self.assertEqual(float, type(result["last_request_timestamp"]))
            self.assertEqual(float, type(result["start_time"]))
            self.assertEqual(int, type(result["num_requests"]))
            self.assertEqual(int, type(result["num_none_requests"]))
            self.assertEqual(float, type(result["total_response_time"]))
            self.assertEqual(float, type(result["max_response_time"]))
            self.assertEqual(float, type(result["min_response_time"]))
            self.assertEqual(int, type(result["total_content_length"]))
            self.assertEqual(dict, type(result["response_times"]))
            self.assertEqual(dict, type(result["num_reqs_per_sec"]))
            self.assertEqual(dict, type(result["num_fail_per_sec"]))

    def test_json_file(self):
        LOCUSTFILE_CONTENT = textwrap.dedent(
            """
            from locust import HttpUser, task, constant

            class QuickstartUser(HttpUser):
                wait_time = constant(1)

                @task
                def hello_world(self):
                    self.client.get("/")

            """
        )
        output_base = "locust_output"
        output_filepath = f"{output_base}.json"
        if os.path.exists(output_filepath):
            os.remove(output_filepath)
        with mock_locustfile(content=LOCUSTFILE_CONTENT) as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--host",
                    "http://google.com",
                    "--headless",
                    "-u",
                    "1",
                    "-t",
                    "2s",
                    "--json-file",
                    output_base,
                ],
                stderr=PIPE,
                stdout=PIPE,
                text=True,
            )
            stdout, stderr = proc.communicate()
            self.assertNotIn("error: argument --json-file: expected one argument", stderr)
            self.assertTrue(os.path.exists(output_filepath))
            with open(output_filepath, encoding="utf-8") as file:
                [stats] = json.load(file)
                self.assertEqual(stats["name"], "/")

        if os.path.exists(output_filepath):
            os.remove(output_filepath)

    @unittest.skipIf(True, reason="This test is too slow for the value it provides")
    def test_worker_indexes(self):
        content = """
from locust import HttpUser, task, between

class AnyUser(HttpUser):
    host = "http://127.0.0.1:8089"
    wait_time = between(0, 0.1)
    @task
    def my_task(self):
        print("worker index:", self.environment.runner.worker_index)
"""
        with mock_locustfile(content=content) as mocked:
            master = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--headless",
                    "--master",
                    "--expect-workers",
                    "2",
                    "-t",
                    "5",
                    "-u",
                    "2",
                    "-L",
                    "DEBUG",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            proc_worker_1 = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--worker",
                    "-L",
                    "DEBUG",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            proc_worker_2 = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--worker",
                    "-L",
                    "DEBUG",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            stdout, stderr = master.communicate()
            self.assertNotIn("Traceback", stderr)
            self.assertIn("Shutting down (exit code 0)", stderr)
            self.assertEqual(0, master.returncode)

            stdout_worker_1, stderr_worker_1 = proc_worker_1.communicate()
            stdout_worker_2, stderr_worker_2 = proc_worker_2.communicate()
            self.assertEqual(0, proc_worker_1.returncode)
            self.assertEqual(0, proc_worker_2.returncode)
            self.assertNotIn("Traceback", stderr_worker_1)
            self.assertNotIn("Traceback", stderr_worker_2)

            PREFIX = "worker index: "
            p1 = stdout_worker_1.find(PREFIX)
            if p1 == -1:
                raise Exception(stdout_worker_1 + stderr_worker_1)
            self.assertNotEqual(-1, p1)
            p2 = stdout_worker_2.find(PREFIX)
            if p2 == -1:
                raise Exception(stdout_worker_2 + stderr_worker_2)
            self.assertNotEqual(-1, p2)
            found = [
                int(stdout_worker_1[p1 + len(PREFIX) :].split("\n")[0]),
                int(stdout_worker_2[p1 + len(PREFIX) :].split("\n")[0]),
            ]
            found.sort()
            for i in range(2):
                if found[i] != i:
                    raise Exception(f"expected index {i} but got", found[i])

    @unittest.skipIf(os.name == "nt", reason="--processes doesnt work on windows")
    def test_processes(self):
        with mock_locustfile() as mocked:
            command = f"locust -f {mocked.file_path} --processes 4 --headless --run-time 1 --exit-code-on-error 0"
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            try:
                _, stderr = proc.communicate(timeout=9)
            except Exception:
                proc.kill()
                assert False, f"locust process never finished: {command}"
            self.assertNotIn("Traceback", stderr)
            self.assertIn("(index 3) reported as ready", stderr)
            self.assertIn("Shutting down (exit code 0)", stderr)

    @unittest.skipIf(os.name == "nt", reason="--processes doesnt work on windows")
    def test_processes_autodetect(self):
        with mock_locustfile() as mocked:
            command = f"locust -f {mocked.file_path} --processes -1 --headless --run-time 1 --exit-code-on-error 0"
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            try:
                _, stderr = proc.communicate(timeout=9)
            except Exception:
                proc.kill()
                assert False, f"locust process never finished: {command}"
            self.assertNotIn("Traceback", stderr)
            self.assertIn("(index 0) reported as ready", stderr)
            self.assertIn("Shutting down (exit code 0)", stderr)

    @unittest.skipIf(os.name == "nt", reason="--processes doesnt work on windows")
    def test_processes_separate_worker(self):
        with mock_locustfile() as mocked:
            master_proc = subprocess.Popen(
                f"locust -f {mocked.file_path} --master --headless --run-time 1 --exit-code-on-error 0 --expect-workers-max-wait 2",
                shell=True,
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )

            worker_parent_proc = subprocess.Popen(
                f"locust -f {mocked.file_path} --processes 4 --worker",
                shell=True,
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )

            try:
                _, worker_stderr = worker_parent_proc.communicate(timeout=9)
            except Exception:
                master_proc.kill()
                worker_parent_proc.kill()
                _, worker_stderr = worker_parent_proc.communicate()
                _, master_stderr = master_proc.communicate()
                assert False, f"worker never finished: {worker_stderr}"

            try:
                _, master_stderr = master_proc.communicate(timeout=9)
            except Exception:
                master_proc.kill()
                worker_parent_proc.kill()
                _, worker_stderr = worker_parent_proc.communicate()
                _, master_stderr = master_proc.communicate()
                assert False, f"master never finished: {master_stderr}"

            _, worker_stderr = worker_parent_proc.communicate()
            _, master_stderr = master_proc.communicate()
            self.assertNotIn("Traceback", worker_stderr)
            self.assertNotIn("Traceback", master_stderr)
            self.assertNotIn("Gave up waiting for workers to connect", master_stderr)
            self.assertIn("(index 3) reported as ready", master_stderr)
            self.assertIn("Shutting down (exit code 0)", master_stderr)

    @unittest.skipIf(os.name == "nt", reason="--processes doesnt work on windows")
    def test_processes_ctrl_c(self):
        with mock_locustfile() as mocked:
            proc = psutil.Popen(  # use psutil.Popen instead of subprocess.Popen to use extra features
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--processes",
                    "4",
                    "--headless",
                    "-L",
                    "DEBUG",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            gevent.sleep(3)
            children = proc.children(recursive=True)
            self.assertEqual(len(children), 4, "unexpected number of child worker processes")

            proc.send_signal(signal.SIGINT)
            gevent.sleep(2)

            for child in children:
                self.assertFalse(child.is_running(), "child processes failed to terminate")

            try:
                _, stderr = proc.communicate(timeout=1)
            except Exception:
                proc.kill()
                _, stderr = proc.communicate()
                assert False, f"locust process never finished: {stderr}"

            self.assertNotIn("Traceback", stderr)
            self.assertIn("(index 3) reported as ready", stderr)
            self.assertIn("The last worker quit, stopping test", stderr)
            self.assertIn("Shutting down (exit code 0)", stderr)
            # ensure no weird escaping in error report. Not really related to ctrl-c...
            self.assertIn(", 'Connection refused') ", stderr)

    @unittest.skipIf(os.name == "nt", reason="--processes doesnt work on windows")
    def test_workers_shut_down_if_master_is_gone(self):
        content = """
from locust import HttpUser, task, constant, runners
runners.MASTER_HEARTBEAT_TIMEOUT = 2

class AnyUser(HttpUser):
    host = "http://127.0.0.1:8089"
    wait_time = constant(1)
    @task
    def my_task(self):
        print("worker index:", self.environment.runner.worker_index)
"""
        with mock_locustfile(content=content) as mocked:
            master_proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--master",
                    "--headless",
                    "--expect-workers",
                    "2",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )

            worker_parent_proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--worker",
                    "--processes",
                    "2",
                    "--headless",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
                start_new_session=True,
            )
            gevent.sleep(2)
            master_proc.kill()
            master_proc.wait()
            try:
                worker_stdout, worker_stderr = worker_parent_proc.communicate(timeout=7)
            except Exception:
                os.killpg(worker_parent_proc.pid, signal.SIGTERM)
                worker_stdout, worker_stderr = worker_parent_proc.communicate()
                assert False, f"worker never finished: {worker_stdout} / {worker_stderr}"

            self.assertNotIn("Traceback", worker_stderr)
            self.assertIn("Didn't get heartbeat from master in over ", worker_stderr)
            self.assertIn("worker index:", worker_stdout)

    @unittest.skipIf(os.name == "nt", reason="--processes doesnt work on windows")
    def test_processes_error_doesnt_blow_up_completely(self):
        with mock_locustfile() as mocked:
            proc = subprocess.Popen(
                [
                    "locust",
                    "-f",
                    mocked.file_path,
                    "--processes",
                    "4",
                    "-L",
                    "DEBUG",
                    "UserThatDoesntExist",
                ],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            _, stderr = proc.communicate()
            self.assertIn("Unknown User(s): UserThatDoesntExist", stderr)
            # the error message should repeat 4 times for the workers and once for the master
            self.assertEqual(stderr.count("Unknown User(s): UserThatDoesntExist"), 5)
            self.assertNotIn("Traceback", stderr)

    @unittest.skipIf(os.name == "nt", reason="--processes doesnt work on windows")
    @unittest.skipIf(sys.platform == "darwin", reason="Flaky on macOS :-/")
    def test_processes_workers_quit_unexpected(self):
        content = """
from locust import runners, events, User, task
import sys
runners.HEARTBEAT_INTERVAL = 0.1

@events.test_start.add_listener
def on_test_start(environment, **_kwargs):
    if isinstance(environment.runner, runners.WorkerRunner):
        sys.exit(42)

class AnyUser(User):
    @task
    def mytask(self):
        pass
"""
        with mock_locustfile(content=content) as mocked:
            worker_proc = subprocess.Popen(
                ["locust", "-f", mocked.file_path, "--processes", "2", "--worker"],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            master_proc = subprocess.Popen(
                ["locust", "-f", mocked.file_path, "--master", "--headless", "-t", "5"],
                stdout=PIPE,
                stderr=PIPE,
                text=True,
            )
            try:
                _, stderr = worker_proc.communicate(timeout=3)
                status_code = worker_proc.wait()
            except Exception:
                worker_proc.kill()
                _, stderr = worker_proc.communicate()
                assert False, f"worker process never finished: {stderr}"
            finally:
                gevent.sleep(4)
                master_proc.kill()
                _, master_stderr = master_proc.communicate()

            self.assertNotIn("Traceback", stderr)
            self.assertIn("INFO/locust.runners: sys.exit(42) called", stderr)
            self.assertEqual(status_code, 42)
            self.assertNotIn("Traceback", master_stderr)
            self.assertIn("failed to send heartbeat, setting state to missing", master_stderr)
