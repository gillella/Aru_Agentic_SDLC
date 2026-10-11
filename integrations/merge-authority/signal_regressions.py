"""Offline signal regressions collected by tests/test_trusted_merge_runtime.py.

Kept separate to respect that module's 800-line limit. Never deliver hardware
fault signals; file-size resource limits are changed only in a disposable child.
"""
from __future__ import annotations

import errno
import importlib.util
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace


@contextmanager
def dispositions(scenario):
    changed = {}
    if scenario == "ignored":
        changed = dict.fromkeys((signal.SIGPIPE, signal.SIGXFSZ, signal.SIGUSR1), signal.SIG_IGN)
    elif scenario == "defaults":
        changed = dict.fromkeys((signal.SIGPIPE, signal.SIGXFSZ), signal.SIG_DFL)
    previous = {number: signal.getsignal(number) for number in changed}
    try:
        for number, handler in changed.items():
            signal.signal(number, handler)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def check_in_guard(scenario, request, helper, head):
    if scenario == "ignored":
        assert all(signal.getsignal(n) == signal.SIG_IGN for n in
                   (signal.SIGPIPE, signal.SIGXFSZ, signal.SIGUSR1))
        os.kill(os.getpid(), signal.SIGUSR1)
    elif scenario == "defaults":
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT,
                       signal.SIGUSR1, signal.SIGUSR2, signal.SIGALRM,
                       signal.SIGPIPE, signal.SIGXFSZ):
            assert callable(signal.getsignal(number))
    elif scenario == "pipe":
        assert signal.getsignal(signal.SIGPIPE) == signal.SIG_IGN
        helper.merge_authority.post(head, "success", "valid")
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            os.write(write_fd, b"broken pipe")
        finally:
            os.close(write_fd)


def check_local(scenario, request, helper, head, monkeypatch):
    if scenario == "faults":
        excluded = {signal.SIGSEGV, signal.SIGBUS, signal.SIGILL, signal.SIGFPE}
    elif scenario == "ignored":
        excluded = {signal.SIGPIPE, signal.SIGXFSZ, signal.SIGUSR1}
    else:
        excluded = set()
    original_handlers = {number: signal.getsignal(number) for number in excluded}
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    original_sigmask = signal.pthread_sigmask
    original_signal = signal.signal
    mask_calls, handler_calls = [], []

    def mask(how, numbers):
        mask_calls.append(set(numbers))
        return original_sigmask(how, numbers)

    def install(number, handler):
        handler_calls.append(number)
        return original_signal(number, handler)

    monkeypatch.setattr(signal, "pthread_sigmask", mask)
    monkeypatch.setattr(signal, "signal", install)
    posts = []
    helper.merge_authority.post = lambda _head, conclusion, _summary: posts.append(conclusion)
    try:
        with request.cancellation_guard(helper, head, request.scope_refusal(helper)):
            active_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
            assert not (excluded & (active_mask - original_mask))
            assert all(signal.getsignal(n) is h for n, h in original_handlers.items())
            check_in_guard(scenario, request, helper, head)
    except BrokenPipeError as exc:
        assert scenario == "pipe" and exc.errno == errno.EPIPE
    else:
        assert scenario != "pipe", "write to a closed pipe did not raise BrokenPipeError"
    assert posts == (["success", "failure"] if scenario == "pipe" else [])
    assert all(not excluded & (numbers - original_mask) for numbers in mask_calls)
    assert not excluded & set(handler_calls)
    assert all(signal.getsignal(n) is h for n, h in original_handlers.items())
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == original_mask


def check_signal_contract(scenario, request, helper, head, tmp_path, monkeypatch):
    if scenario == "file-size":
        child = subprocess.run(
            [sys.executable, "-I", __file__, request.__file__, str(tmp_path / "limited")],
            capture_output=True, text=True, timeout=10,
        )
        assert child.returncode == 0, child.stdout + child.stderr
    else:
        with dispositions(scenario):
            check_local(scenario, request, helper, head, monkeypatch)


def file_size_child(request_path, target):
    spec = importlib.util.spec_from_file_location("request_under_test", request_path)
    request = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(request)
    helper = SimpleNamespace(KernelError=RuntimeError, merge_authority=SimpleNamespace(post=None))
    posts = []
    helper.merge_authority.post = lambda _head, conclusion, _summary: posts.append(conclusion)
    original = signal.getsignal(signal.SIGXFSZ)
    assert original == signal.SIG_IGN
    resource.setrlimit(resource.RLIMIT_FSIZE, (1, 1))
    try:
        with request.cancellation_guard(helper, "a" * 40, request.scope_refusal(helper)):
            assert signal.getsignal(signal.SIGXFSZ) == signal.SIG_IGN
            helper.merge_authority.post("a" * 40, "success", "valid")
            fd = os.open(target, os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                assert os.write(fd, b"a") == 1
                os.write(fd, b"b")
            finally:
                os.close(fd)
    except OSError as exc:
        assert exc.errno == errno.EFBIG
    else:
        raise AssertionError("file-size limit did not raise OSError")
    assert posts == ["success", "failure"]
    assert signal.getsignal(signal.SIGXFSZ) is original


if __name__ == "__main__":
    file_size_child(sys.argv[1], Path(sys.argv[2]))
