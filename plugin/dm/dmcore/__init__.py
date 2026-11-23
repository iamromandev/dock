"""dm core: download logic, kept free of protocol code so it can be unit tested."""

import os
import sys
import threading


class Cancelled(Exception):
    pass


def _peak_rss_kb():
    import resource

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss // 1024 if sys.platform == "darwin" else rss  # Linux reports KiB


def run(inputs, cwd, notify, cancelled):
    """Run one dm invocation. Returns the exit code; raises Cancelled on cancel."""
    from . import fetch
    from .fmt import human_size
    from .target import DmError, resolve

    handle = fetch.Handle()
    result = {}
    done = threading.Event()

    def progress(percent, message):
        notify("progress", percent=percent, message=message)

    def work(target):
        try:
            result["ok"] = fetch.download(inputs["url"], target, progress, cancelled, handle)
        except BaseException as e:  # reported by the main thread
            result["err"] = e
        finally:
            done.set()

    try:
        target = resolve(cwd, inputs.get("output"), inputs.get("dir"))
        fetch.check_url(inputs["url"])
        threading.Thread(target=work, args=(target,), daemon=True).start()
        while not done.wait(0.05):
            if cancelled.is_set():
                if handle.abort():
                    raise Cancelled()
                done.wait()  # already moved into place: report the success
        err = result.get("err")
        if err is not None:
            raise err
        path, size = result["ok"]
        rel = os.path.relpath(path, os.path.realpath(cwd))
        notify("output", text=f"saved {rel} ({human_size(size)})\n")
        notify("log", level="debug", message=f"peak_rss_kb={_peak_rss_kb()}")
        return 0
    except DmError as e:
        notify("log", level="error", message=f"dm: {e}")
        return e.code
    except Cancelled:
        raise
    except Exception as e:  # its text may hold the URL: show the class only
        notify("log", level="error", message=f"dm: {type(e).__name__}")
        return 1
