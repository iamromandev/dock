"""Choosing a safe file name for a download."""

import re
from email.message import EmailMessage
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

MAX_BYTES = 255
_DEFAULT = "download"
# control chars, plus bidi controls (e.g. U+202E) as defense in depth
_BAD = re.compile("[\x00-\x1f\x7f‎‏؜‪-‮⁦-⁩]")
_COMPRESSORS = {".gz", ".bz2", ".xz", ".zst", ".lz", ".lzma", ".z"}


def _cut(text, limit):
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _split(name):
    """(stem, ext): ext is the last suffix, or '.tar.gz' style for tarballs."""
    p = PurePosixPath(name)
    suffixes = p.suffixes
    if not suffixes or (name.startswith(".") and name.count(".") == 1):
        return name, ""
    ext = suffixes[-1]
    if ext.lower() in _COMPRESSORS and len(suffixes) > 1 and suffixes[-2].lower() == ".tar":
        ext = suffixes[-2] + ext
    return name[: len(name) - len(ext)], ext


def _fit(stem, ext, tail=""):
    """stem + tail + ext within MAX_BYTES, cutting the stem first."""
    room = MAX_BYTES - len((tail + ext).encode("utf-8"))
    if room < 1:  # absurd extension: cut the whole thing
        return _cut(stem + tail + ext, MAX_BYTES)
    return _cut(stem, room) + tail + ext


def safe_name(raw):
    name = re.split(r"[/\\]", raw)[-1]
    name = _BAD.sub("", name).lstrip(".").strip(" ")
    if len(name.encode("utf-8")) > MAX_BYTES:
        stem, ext = _split(name)
        name = _fit(stem, ext).strip(" ")
    return name if name not in ("", ".", "..") else _DEFAULT


_SPLIT = re.compile(r';(?=(?:[^"]*"[^"]*")*[^"]*$)')


def _params(value):
    for part in _SPLIT.split(value)[1:]:
        key, eq, val = part.partition("=")
        if eq:
            yield key.strip().lower(), val.strip()


def _rfc5987(val):
    """Decode charset'lang'pct-encoded; None if the charset is unknown or the form is bad."""
    charset, _, rest = val.partition("'")
    _, quote, text = rest.partition("'")
    if not quote:
        return None
    try:
        return unquote(text, encoding=charset or "utf-8", errors="replace")
    except LookupError:
        return None


def _header_name(value):
    """filename* (RFC 5987) beats filename, whatever the order."""
    star = plain = None
    for key, val in _params(value):
        if key == "filename*" and star is None:
            star = _rfc5987(val)
        elif key == "filename" and plain is None:
            if len(val) >= 2 and val[0] == val[-1] == '"':
                val = re.sub(r"\\(.)", r"\1", val[1:-1])
            plain = val
    if star:
        return star
    if plain:
        return plain
    msg = EmailMessage()  # continuations (filename*0*=...) and odd forms
    msg["Content-Disposition"] = value
    return msg.get_filename()


def name_from(headers, final_url):
    raw = None
    value = headers.get("Content-Disposition")
    if value:
        try:
            raw = _header_name(value)
        except Exception:
            raw = None
    if not raw:
        raw = unquote(urlsplit(final_url).path.rpartition("/")[2])
    return safe_name(raw)


def candidates(name, is_dir=False):
    """name, then 'stem (1).ext' ... 'stem (999).ext'; a folder has no extension to keep."""
    yield name
    stem, ext = (name, "") if is_dir else _split(name)
    for i in range(1, 1000):
        yield _fit(stem, ext, f" ({i})")
