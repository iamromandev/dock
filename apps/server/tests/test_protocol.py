import pytest

from src import protocol


@pytest.mark.parametrize("text", ["not json", "[1]", '"x"', "{}", '{"type": "nope"}', '{"type": 3}'])
def test_parse_rejects_bad_messages_with_bad_message(text):
    # bug: junk or an unknown type escapes as a JSON/KeyError exception and kills the connection
    with pytest.raises(protocol.ProtocolError) as e:
        protocol.parse(text)
    assert e.value.code == "bad_message"

