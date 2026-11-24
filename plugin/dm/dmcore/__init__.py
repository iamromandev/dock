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


def torrent_path(cwd, arg):
    """The .torrent file arg names, or None when arg is not one (then the HTTP rules apply).

    A torrent is an existing regular file ending in .torrent (any case) that resolves inside cwd.
    """
    from .target import DmError, _inside

    if not arg.lower().endswith(".torrent") or "\x00" in arg:
        return None
    candidate = os.path.join(cwd, arg)
    if not os.path.isfile(candidate):
        return None
    real = os.path.realpath(candidate)
    if not _inside(os.path.realpath(cwd), real):
        raise DmError(f"{arg} is outside the current folder")
    return real


def save_torrent(final, target, info, trackers):
    """Write `<name>.torrent` for a finished magnet download beside it, never over anything.

    Returns the path written. OSError or DmError when it cannot be (the download itself is fine then).
    """
    import secrets

    from .names import _cut, _fit
    from .torrent import metainfo
    from .torrent.place import place_file

    name = _fit(target.fixed_name or final.name, ".torrent")
    tmp = target.folder / f".{_cut(name, 100)}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o666)
        with os.fdopen(fd, "wb") as f:
            f.write(metainfo.torrent_bytes(info, trackers))
        return place_file(tmp, target.folder, name), name
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run(inputs, cwd, notify, cancelled):
    """Run one dm invocation. Returns the exit code; raises Cancelled on cancel."""
    from . import fetch
    from .fmt import human_size
    from .names import _fit
    from .target import DmError, resolve
    from .torrent import magnet

    arg = inputs["url"]
    handle = fetch.Handle()
    result = {}
    done = threading.Event()

    def progress(percent, message):
        notify("progress", percent=percent, message=message)

    def work(target):
        try:
            result["ok"] = fetch.download(arg, target, progress, cancelled, handle,
                                          resume=not inputs.get("no_resume"),
                                          note=lambda text: notify("output", text=text + "\n"))
        except BaseException as e:  # reported by the main thread
            result["err"] = e
        finally:
            done.set()

    def work_torrent(path, target, opts):
        from .torrent.session import run_torrent

        try:
            result["ok"] = run_torrent(path, target, progress, handle, opts,
                                       lambda m: notify("log", level="debug", message=m),
                                       resume=not inputs.get("no_resume"),
                                       note=lambda text: notify("output", text=text + "\n"))
        except DmError as e:  # which torrent failed is the part worth showing
            result["err"] = DmError(f"{arg}: {e}", e.code, e.keep)
        except BaseException as e:
            result["err"] = e
        finally:
            done.set()

    def work_magnet(link, target, opts):
        from .torrent.session import run_magnet

        label = magnet.label(link.info_hash)
        try:
            final, size, nfiles, info = run_magnet(link, target, progress, handle, opts,
                                                   lambda m: notify("log", level="debug", message=m),
                                                   resume=not inputs.get("no_resume"),
                                                   note=lambda text: notify("output", text=text + "\n"))
            result["ok"] = (final, size, nfiles)
            try:  # the download is in place: failing to write the .torrent only warns
                saved, name = save_torrent(final, target, info, link.trackers)
                result["saved"] = saved
            except Exception as e:  # whatever went wrong, the download itself succeeded
                name = _fit(target.fixed_name or final.name, ".torrent")
                reason = (e.strerror if isinstance(e, OSError) else str(e)) or type(e).__name__
                notify("log", level="warn", message=f"could not save {name}: {reason}")
        except DmError as e:  # which magnet failed is the part worth showing
            result["err"] = DmError(f"{label}: {e}", e.code, e.keep)
        except BaseException as e:
            result["err"] = e
        finally:
            done.set()

    try:
        target = resolve(cwd, inputs.get("output"), inputs.get("dir"))
        link = None
        if magnet.is_magnet(arg):
            try:
                link = magnet.parse(arg)
            except magnet.MagnetError as e:  # the label shows the hash only, never the link
                text = f"{magnet.label(e.info_hash)}: {e}" if e.info_hash else str(e)
                raise DmError(text, e.code) from None
        torrent = torrent_path(cwd, arg) if link is None else None
        if link is not None or torrent is not None:
            from .torrent.handle import TorrentHandle
            from .torrent.session import Options

            stall = inputs.get("stall")
            if stall is not None and (isinstance(stall, bool) or not isinstance(stall, int) or stall < 1):
                raise DmError("--stall-seconds must be at least 1", 2)
            opts = Options() if stall is None else Options(stall_s=stall)
            handle = TorrentHandle()
            if link is not None:
                threading.Thread(target=work_magnet, args=(link, target, opts), daemon=True).start()
            else:
                threading.Thread(target=work_torrent, args=(torrent, target, opts), daemon=True).start()
        else:
            fetch.check_url(arg)
            threading.Thread(target=work, args=(target,), daemon=True).start()
        while not done.wait(0.05):
            if cancelled.is_set():
                if handle.abort():
                    done.wait(1)  # the worker decides what to keep and frees the lock as soon as it is woken
                    raise Cancelled()
                done.wait()  # already moved into place: report the success
        err = result.get("err")
        if err is not None:
            raise err
        path, size, *files = result["ok"]
        rel = os.path.relpath(path, os.path.realpath(cwd))
        count = f", {files[0]} file{'s' if files[0] != 1 else ''}" if files else ""
        notify("output", text=f"saved {rel} ({human_size(size)}{count})\n")
        if "saved" in result:
            notify("output", text=f"saved {os.path.relpath(result['saved'], os.path.realpath(cwd))}\n")
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
