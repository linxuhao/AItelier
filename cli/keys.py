# cli/keys.py
# Background keyboard listener using stdlib termios (Unix only).
# Falls back to no-op on Windows or when stdin is not a TTY.

import sys
import threading


def start_key_listener(renderer) -> threading.Thread:
    """
    Start a daemon thread that reads single keypresses and updates
    the renderer's active_view. No external dependencies required.
    """
    def _listen_unix():
        import tty
        import termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while not renderer.pipeline_ended:
                ch = sys.stdin.read(1)
                if ch in ("q", "Q", "\x03"):  # q or Ctrl+C
                    renderer.pipeline_ended = True
                elif "1" <= ch <= "6":
                    idx = int(ch) - 1
                    if idx < len(renderer.steps):
                        renderer.active_view = idx
        except (OSError, ValueError):
            pass  # stdin closed or not available
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except (OSError, ValueError):
                pass

    def _listen_noop():
        pass

    if not sys.stdin.isatty():
        t = threading.Thread(target=_listen_noop, daemon=True)
        t.start()
        return t

    try:
        import termios  # noqa: F401 — test availability
    except ImportError:
        t = threading.Thread(target=_listen_noop, daemon=True)
        t.start()
        return t

    t = threading.Thread(target=_listen_unix, daemon=True)
    t.start()
    return t
