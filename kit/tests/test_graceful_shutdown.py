from __future__ import annotations

import signal

import pytest

from kit.app import App, _GracefulStop


def test_term_handler_uses_baseexception_and_sets_stop_flag():
    app = App()
    assert app._stop_flag is False
    with pytest.raises(_GracefulStop) as caught:
        app._on_stop_signal(signal.SIGTERM, None)
    assert caught.value.signum == signal.SIGTERM
    assert app._stop_flag is True
    assert not isinstance(caught.value, Exception)


def test_stop_handlers_are_restored_by_finish_without_started_runtime():
    app = App()
    before_term = signal.getsignal(signal.SIGTERM)
    before_int = signal.getsignal(signal.SIGINT)
    try:
        app._install_stop_handlers()
        assert getattr(signal.getsignal(signal.SIGTERM), "__self__", None) is app
        assert getattr(signal.getsignal(signal.SIGINT), "__self__", None) is app
        app.finish()
        assert signal.getsignal(signal.SIGTERM) == before_term
        assert signal.getsignal(signal.SIGINT) == before_int
    finally:
        signal.signal(signal.SIGTERM, before_term)
        signal.signal(signal.SIGINT, before_int)
