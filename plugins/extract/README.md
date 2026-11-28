# dev.dock.extract

Unpacks an archive from the in folder into a new folder in the current folder, refusing anything that could write outside it. Python standard library only; no network.

## Use

```
dock extract backup.tar.gz [-d name] [--list] [--only patterns]
```

- `file` (required): the archive, resolved against the in folder (an absolute path stays absolute) and read only from inside the in folder or, in a flow, the flow's work folder. A symlink is followed only if it ends inside one of those; the final component is opened without following a link and without blocking (a FIFO is refused).
- `--dir` / `-d`: the new folder's name: a plain name (no `/`, `\`, NUL, not `.` or `..`, at most 255 bytes) that must not exist yet. Default: the archive's name without its suffix; if that is taken, `name-2`, `name-3` and so on; `archive` if nothing is left.
- `--list`: shows what an unpack would do and writes nothing (no destination, no temp folder). One `table` (`entries`) with the columns `name`, `size`, `type` (`file`, `dir` or `link`) and `note`: `refused: <reason>` for an entry the safety checks would refuse (the listing still ends with exit code 0), `encrypted` for an encrypted zip entry, else empty; a `size` that is unknown (a single compressed file) is an empty cell; an entry whose body cannot be read to check it (a zip symlink with an unsupported compression method) is marked `refused: unsupported zip compression`. At most 1000 rows; an info log says `… N more entries not shown`. The result is `{"exit_code": 0, "outputs": {"entries": n}}` with `n` counting every listed entry, shown or not. The entry limit, the tar header cap and the byte and ratio limits apply as for an unpack. A password is never asked.
- `--only`: comma-separated glob patterns (`fnmatch`, case-sensitive; `*` also matches `/`, so `*.md` finds `docs/a.md`). Spaces around a pattern and empty items are dropped. Only entries whose normalised name matches are extracted, with their parent folders; a link is kept only if it matches itself, and a hard link whose target was left out is refused. Every entry is still checked, including those left out: a hostile name anywhere fails the run. No match is exit code 1, `no entry matches --only`. With `--list`, only matching entries are listed (an entry whose name cannot be normalised is matched by its raw name). So `--list --only` hides a hostile entry outside the filter, while a real run with the same `--only` still refuses it.

## Formats

Found by content, not by suffix: zip, tar, and tar compressed with gzip, bzip2, xz or zstd. A single file compressed with gzip, bzip2, xz or zstd (not a tar) becomes one file named after the archive without the compression suffix: `n.txt.gz` gives `n.txt/n.txt`. Anything else is `unsupported archive format`.

## Passwords

A zip with ZipCrypto-encrypted entries (the old zip scheme) makes the plugin send the host one `form` request, `Password for <archive>`, with one secret `text` field, before anything is unpacked; it is asked once per run, and only when an entry that would be extracted (after `--only`) is encrypted; a run refused anyway (declared sizes over the limit, no `--only` match) is refused before the question. An empty answer or no answer (end of input) is `no password given`; a wrong one is `wrong password` (found on any encrypted entry, as a bad check byte or a CRC mismatch) and leaves nothing behind. A password that cannot be encoded (a lone surrogate) is `no password given`. PKWARE strong encryption, which the standard library cannot read, is reported as `wrong password` too. The password is never logged, returned or put in an error message. ZipCrypto is weak (known-plaintext attacks break it); it is supported only so old archives can be read. AES-encrypted zips (compression method 99) are not supported: `AES-encrypted zips are not supported`, refused before any question. `--list` never asks for a password. A cancel while the form is open answers `-32800`.

## Limits

| Env var | Default | Meaning |
|---|---|---|
| `DOCK_EXTRACT_MAX_BYTES` | 10 GiB | total bytes written |
| `DOCK_EXTRACT_MAX_ENTRIES` | 100000 | entries in the archive (files, folders, links) |

A value that is not a positive integer is an error. The limits count the bytes really written (checked per 1 MiB block), not the sizes the archive declares; the one use of declared sizes is that a zip is also refused up front when the declared sizes of its entries add up to more than `DOCK_EXTRACT_MAX_BYTES` (and when it has more entries than `DOCK_EXTRACT_MAX_ENTRIES`), before anything is written. For a tar, everything read from the decompressed stream counts, headers and skipped data too. A compression ratio over 1000:1 also stops the run: per zip entry over 1 MiB, and for tar and single-file archives over the whole stream once more than 1 MiB is read or written. The error names the limit.

Fixed limits:

- A name may have at most 128 components (`a/b/c`: 3); a deeper one fails the run. Folders made implicitly as parents count against `DOCK_EXTRACT_MAX_ENTRIES` too.
- The headers of one tar member (long names and PAX records, which tarfile holds whole in memory) may add up to 4 MiB; more fails the run.

## Safety

- Everything is written into a private `.extract-<random>.part` folder (mode 0700) in the current folder and renamed to the destination only when every entry is written. A failure or cancel removes only that folder; other `.part` folders are never touched. Nothing partial is ever left under the destination name. If the plugin is killed (SIGKILL, a crash, power loss) it cannot clean up, and its `.extract-*.part` folder stays in the current folder; it is safe to delete by hand.
- Entry names are normalised (`\` becomes `/`, a leading `./` is dropped). An absolute path, a drive letter (`C:`), any `..` part, a NUL, a name over 4096 bytes or a part over 255 bytes fails the whole run, as does a name that is repeated or differs only in case (`A.txt` and `a.txt`), a file that is also a parent folder, or a non-folder entry named like the archive root (`./`). A zip name is read as stored, so a NUL in it is refused, not cut.
- Symlinks and tar hard links are made only after all files and folders, and only if the target stays inside the destination (no absolute target; `..` only as leading parts that stay inside; a hard link must point to a regular file already unpacked). Files are written through directory file descriptors opened without following links (`O_NOFOLLOW`, `O_EXCL`), so nothing is written through a link; an entry under the name of a symlink entry fails.
- Devices, FIFOs and sockets fail the run. setuid, setgid and sticky bits are dropped; other permission bits and modification times are kept (the owner can always read and write files and enter folders); the owner is not restored.

## Outputs

`{"exit_code": 0, "outputs": {"dir": "/abs/dest", "files": ["/abs/dest/a.txt", ...], "truncated": false}}`: `files` lists the regular files in archive order, at most 1000 (`truncated` is true when there were more). An info log says `extracted N files (size) into name/` (`1 file`, `1 link` when one), or `extracted N files and M links (size) into name/` when links were made (`files` still lists regular files only). A bar `extract` ("extracting") shows written / declared total (zip only; `null` for tar and single files), at most 5 a second, and ends with 100 `done`.

## Errors

Wrong input types are JSON-RPC errors (`-32602`). A bad value, a refused or unsupported archive, a limit, or a missing destination is logged as an error and the result is `{"exit_code": 1}`. Damaged data (bad zip or tar, a truncated or corrupt compressed stream, a tar whose first header does not parse when it has the `ustar` magic or a tar name such as `.tar.gz`) is `archive is damaged: <reason>`; a zip method the standard library cannot read is `unsupported zip compression`; a full disk is `no space left`; a destination that appeared meanwhile is `<name> already exists`. A cancel, or closing stdin, during a run stops before the next block, removes the temp folder and answers `-32800`; a cancel with nothing running is ignored.

Permissions: `fs:read:in`, `fs:write:cwd`.

## Develop

`make extract-test`, `make extract-check-ci` (the tests drive `main.py` over stdio; no Docker needed).
