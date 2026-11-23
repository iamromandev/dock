"""Strict bencode: decoding rejects anything a canonical encoder would not produce."""


class BencodeError(ValueError):
    pass


MAX_INT_DIGITS = 20


def _spend(budget):
    budget[0] -= 1
    if budget[0] < 0:
        raise BencodeError("too many items")


def decode(data, max_depth=64, max_nodes=2_000_000):
    """(value, span): span is the (start, end) of the top-level dict's b"info" value, or None.

    max_nodes caps the total number of lists, dicts, integers and strings (keys included).
    """
    data = bytes(data)
    pos, value, span = _value(data, 0, 0, max_depth, [max_nodes], True)
    if pos != len(data):
        raise BencodeError("trailing data")
    return value, span


def _int_end(data, pos):
    end = data.find(b"e", pos)
    if end < 0:
        raise BencodeError("truncated integer")
    return end


def _number(text, allow_neg):
    body = text[1:] if allow_neg and text[:1] == b"-" else text
    if not body or not body.isdigit() or not body.isascii():
        raise BencodeError("bad integer")
    if len(body) > MAX_INT_DIGITS:
        raise BencodeError("integer too large")
    if body[:1] == b"0" and (len(body) > 1 or body is not text):
        raise BencodeError("bad integer")  # leading zero or -0
    return int(text)


def _string(data, pos):
    colon = data.find(b":", pos, pos + 21)
    if colon < 0:
        raise BencodeError("bad string length")
    size = _number(data[pos:colon], False)
    end = colon + 1 + size
    if end > len(data):
        raise BencodeError("truncated string")
    return end, data[colon + 1:end]


def _value(data, pos, depth, max_depth, budget, top=False):
    _spend(budget)
    if pos >= len(data):
        raise BencodeError("truncated input")
    c = data[pos:pos + 1]
    if c == b"i":
        end = _int_end(data, pos + 1)
        return end + 1, _number(data[pos + 1:end], True), None
    if c.isdigit():
        end, s = _string(data, pos)
        return end, s, None
    if c not in (b"l", b"d"):
        raise BencodeError("unexpected byte")
    if depth + 1 > max_depth:
        raise BencodeError("nesting too deep")
    pos += 1
    if c == b"l":
        items = []
        while True:
            if pos >= len(data):
                raise BencodeError("truncated list")
            if data[pos:pos + 1] == b"e":
                return pos + 1, items, None
            pos, item, _ = _value(data, pos, depth + 1, max_depth, budget)
            items.append(item)
    out = {}
    span = None
    last = None
    while True:
        if pos >= len(data):
            raise BencodeError("truncated dict")
        if data[pos:pos + 1] == b"e":
            return pos + 1, out, span
        if not data[pos:pos + 1].isdigit():
            raise BencodeError("dict key is not a string")
        _spend(budget)
        pos, key = _string(data, pos)
        if last is not None and key <= last:
            raise BencodeError("dict keys not strictly ascending")
        last = key
        start = pos
        pos, out[key], _ = _value(data, pos, depth + 1, max_depth, budget)
        if top and key == b"info":
            span = (start, pos)


def encode(value):
    out = bytearray()
    _encode(value, out)
    return bytes(out)


def _encode(value, out):
    if isinstance(value, bool):
        raise TypeError("bool is not bencodable")
    if isinstance(value, int):
        out += b"i%de" % value
    elif isinstance(value, (bytes, bytearray)):
        out += b"%d:" % len(value) + bytes(value)
    elif isinstance(value, str):
        _encode(value.encode("utf-8"), out)
    elif isinstance(value, (list, tuple)):
        out += b"l"
        for item in value:
            _encode(item, out)
        out += b"e"
    elif isinstance(value, dict):
        items = sorted((k.encode("utf-8") if isinstance(k, str) else bytes(k), v)
                       for k, v in value.items())
        out += b"d"
        for k, v in items:
            _encode(k, out)
            _encode(v, out)
        out += b"e"
    else:
        raise TypeError(f"cannot bencode {type(value).__name__}")
