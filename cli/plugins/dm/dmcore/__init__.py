"""dm core: download logic, kept free of protocol code so it can be unit tested."""

import os
import sys
import threading
from pathlib import Path


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


def run(inputs, cwd, notify, cancelled, data_dir=None):
    """Run one dm invocation. Returns the exit code; raises Cancelled on cancel.

    data_dir is the plugin's own folder: dm keeps one file there, the DHT node cache."""
    from . import fetch
    from .fmt import human_size
    from .names import _fit
    from .target import DmError, resolve
    from .torrent import magnet, select

    arg = inputs["url"]
    handle = fetch.Handle()
    result = {}
    done = threading.Event()

    last = {}  # the newest percent and message: the bar is closed with them when the download succeeds
    view = None  # a torrent run: its bars, panel and table (see view.py); an HTTP download has the one bar

    def progress(percent, message):
        if view is not None:
            view.progress(percent, message)
            return
        last.update(percent=percent, message=message)
        notify("bar", id="dm", label="", percent=percent, message=message)

    def drop_bar():
        """A download that did not finish leaves no bar behind: only the error line (or the cancel) shows."""
        if view is not None:
            view.drop()
        elif last:
            notify("clear", id="dm", kind="bar")

    def work(target):
        try:
            result["ok"] = fetch.download(arg, target, progress, cancelled, handle,
                                          resume=not inputs.get("no_resume"),
                                          note=lambda text: notify("output", text=text + "\n"))
        except BaseException as e:  # reported by the main thread
            result["err"] = e
        finally:
            done.set()

    def show(meta, picks):
        """Print the files of meta (those picked, when there are picks) and their total."""
        from .torrent import layout

        specs = layout.plan_files(meta)
        chosen = select.resolve(specs, picks) if picks is not None else None
        for line in select.list_lines(specs, chosen):
            notify("output", text=line + "\n")

    def work_list(link, target, opts, picks):
        from .torrent.session import list_magnet

        label = magnet.label(link.info_hash)
        try:
            show(list_magnet(link, target, handle, opts, lambda m: notify("log", level="debug", message=m),
                             progress), picks)
            result["listed"] = True
        except DmError as e:
            result["err"] = DmError(f"{label}: {e}", e.code, e.keep)
        except BaseException as e:
            result["err"] = e
        finally:
            done.set()

    def work_torrent(path, target, opts, picks):
        from .torrent.session import run_torrent

        try:
            result["ok"] = run_torrent(path, target, progress, handle, opts,
                                       lambda m: notify("log", level="debug", message=m),
                                       resume=not inputs.get("no_resume"),
                                       note=lambda text: notify("output", text=text + "\n"),
                                       on_stats=view.stats, selection=picks)
        except DmError as e:  # which torrent failed is the part worth showing
            result["err"] = DmError(f"{arg}: {e}", e.code, e.keep)
        except BaseException as e:
            result["err"] = e
        finally:
            done.set()

    def work_magnet(link, target, opts, picks):
        from .torrent.session import run_magnet

        label = magnet.label(link.info_hash)
        try:
            final, size, nfiles, info = run_magnet(link, target, progress, handle, opts,
                                                   lambda m: notify("log", level="debug", message=m),
                                                   resume=not inputs.get("no_resume"),
                                                   note=lambda text: notify("output", text=text + "\n"),
                                                   on_stats=view.stats, selection=picks)
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
        dht = not inputs.get("no_dht")
        if magnet.is_magnet(arg):
            try:
                link = magnet.parse(arg)
                magnet.require_sources(link, dht)
            except magnet.MagnetError as e:  # the label shows the hash only, never the link
                text = f"{magnet.label(e.info_hash)}: {e}" if e.info_hash else str(e)
                raise DmError(text, e.code) from None
        torrent = torrent_path(cwd, arg) if link is None else None
        only, skip, listing = inputs.get("only"), inputs.get("skip"), bool(inputs.get("list"))
        if link is None and torrent is None and (only is not None or skip is not None or listing) \
                and arg.lower().startswith(("http://", "https://")):  # anything else keeps its usual error
            raise DmError("--only, --skip and --list are for torrents and magnets")
        if link is not None or torrent is not None:
            label = magnet.label(link.info_hash) if link is not None else arg
            try:  # a bad pick fails before the network or the folder is touched
                picks = select.parse(only, skip)
                if listing and link is None:
                    from .torrent import metainfo

                    show(metainfo.load(torrent), picks)
                    return 0
            except DmError as e:
                raise DmError(f"{label}: {e}", e.code, e.keep) from None
            from .torrent.dht.cache import CACHE_NAME
            from .torrent.handle import TorrentHandle
            from .torrent.session import Options
            from .view import TorrentView

            view = TorrentView(notify)
            stall = inputs.get("stall")
            if stall is not None and (isinstance(stall, bool) or not isinstance(stall, int) or stall < 1):
                raise DmError("--stall-seconds must be at least 1", 2)
            kw = {} if stall is None else {"stall_s": stall}
            if dht and data_dir:
                kw["dht_cache"] = Path(data_dir) / CACHE_NAME
            opts = Options(dht=dht, **kw)
            handle = TorrentHandle()
            if link is not None and listing:
                threading.Thread(target=work_list, args=(link, target, opts, picks), daemon=True).start()
            elif link is not None:
                threading.Thread(target=work_magnet, args=(link, target, opts, picks), daemon=True).start()
            else:
                threading.Thread(target=work_torrent, args=(torrent, target, opts, picks),
                                 daemon=True).start()
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
        if result.get("listed"):
            drop_bar()
            return 0
        path, size, *files = result["ok"]
        rel = os.path.relpath(path, os.path.realpath(cwd))
        count = f", {files[0]} file{'s' if files[0] != 1 else ''}" if files else ""
        if view is not None:
            view.finish()
        elif last:
            notify("bar", id="dm", label="", done=True, **last)
        notify("output", text=f"saved {rel} ({human_size(size)}{count})\n")
        if "saved" in result:
            notify("output", text=f"saved {os.path.relpath(result['saved'], os.path.realpath(cwd))}\n")
        notify("log", level="debug", message=f"peak_rss_kb={_peak_rss_kb()}")
        return 0
    except DmError as e:
        drop_bar()
        notify("log", level="error", message=f"dm: {e}")
        return e.code
    except Cancelled:
        drop_bar()
        raise
    except Exception as e:  # its text may hold the URL: show the class only
        drop_bar()
        notify("log", level="error", message=f"dm: {type(e).__name__}")
        return 1
