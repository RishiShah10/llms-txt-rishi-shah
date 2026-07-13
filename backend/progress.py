from dataclasses import dataclass


@dataclass
class Event:
    """One line of pipeline activity, streamed to the browser.

    `phase` is "start" or "done" for anything that blocks (so the log announces
    an operation before it stalls, not after), and "info" for one-shot markers.
    """

    stage: str
    phase: str = "info"
    url: str | None = None
    status: int | None = None
    ok: bool | None = None
    done: int | None = None      # monotonic admitted-page count (stage="page" only)
    total: int | None = None
    message: str | None = None
    # True when this fetch fell back to the paid browser -- info for the log, not
    # a warning.
    browser: bool = False


def noop(event: Event) -> None:
    """Default emitter. Keeps every non-streaming caller unchanged."""


def emit(on_event, event: Event) -> None:
    """Instrumentation must never change the output.

    Every on_event call routes through here, so a broken emitter (e.g. a write to a
    closed socket) can't alter the generated llms.txt or its persistence -- a raise
    would otherwise be swallowed by _extract_pages and silently drop a page.
    CancelledError is BaseException, so it still propagates.
    """
    try:
        on_event(event)
    except Exception:  # noqa: BLE001
        pass
