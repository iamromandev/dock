import threading

import pytest
from dock_host import uimsg

from src.asks import Asks
from src.remote_ui import RemoteUI

BIDI = "‮"


def make():
    frames: list[dict] = []
    return frames, RemoteUI(frames.append, "r1", Asks())


def test_output_text_is_sanitised_before_it_is_sent():
    # bug: a plugin's bidi override or escape reaches every client raw
    frames, ui = make()
    ui.output({"text": f"a{BIDI}b\x1b[31m\tc\n"})
    assert frames[0]["params"] == {"text": "a�b�[31m    c\n"}


def test_bar_is_normalised_and_an_invalid_one_raises_invalid():
    # bug: clients get a bar without label or message, or an invalid message is forwarded instead of refused
    frames, ui = make()
    ui.bar({"id": "b", "percent": 250})
    assert frames[0]["params"] == {"id": "b", "label": "", "percent": 100.0, "message": "", "done": False}
    with pytest.raises(uimsg.Invalid):
        ui.bar({"label": "no id"})
    assert len(frames) == 1


def test_form_sends_display_choices_only_and_maps_the_answer_back_to_the_raw_value():
    # bug: the client sees raw (unsanitised) choice values, or its display answer reaches the plugin instead of the raw one
    frames, ui = make()
    raw = f"eu{BIDI}1"
    fields = [{"name": "m", "label": "M", "type": "choice", "choices": [raw, "us"], "default": raw}]
    out = {}
    t = threading.Thread(target=lambda: out.update(ui.form({"title": "T", "fields": fields})), daemon=True)
    t.start()
    while not frames:
        t.join(0.01)
    sent = frames[0]["params"]["fields"][0]
    assert sent["choices"] == ["eu�1", "us"] and sent["default"] == "eu�1"
    assert "values" not in sent and "raw_default" not in sent and BIDI not in str(frames[0])
    assert ui.asks.answer(frames[0]["ask"], {"values": {"m": "eu�1"}})
    t.join(5)
    assert out == {"values": {"m": raw}}
