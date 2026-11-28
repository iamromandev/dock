"""The stdio loop, driven through the sample plugin."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from sample_plugin import Plugin

KIT = Path(__file__).resolve().parent.parent


@pytest.fixture
def plugin():
    p = Plugin()
    yield p
    p.close()


def logs(p):
    return [m["params"]["message"] for m in p.seen if m.get("method") == "log"]


def test_initialize_names_the_plugin_and_shutdown_ends_it(plugin):
    plugin.send(id=1, method="initialize", params={})
    assert plugin.answer(1)["result"] == {"name": "sample"}
    plugin.send(id=2, method="shutdown")
    assert plugin.answer(2)["result"] == {}
    assert plugin.proc.wait(10) == 0


def test_an_invoke_answers_exit_0_with_the_outputs(plugin):
    assert plugin.invoke("echo", text="hi")["result"] == {"exit_code": 0, "outputs": {"text": "hi", "tag": None}}


def test_unknown_command_and_method_are_32601(plugin):
    assert plugin.invoke("nope")["error"]["code"] == -32601
    plugin.send(id=9, method="frobnicate")
    assert plugin.answer(9)["error"] == {"code": -32601, "message": "unknown method frobnicate"}


@pytest.mark.parametrize(("command", "inputs", "extra", "message"), [
    ("echo", {}, {}, "sample needs the input text"),
    ("echo", {"text": "x", "tag": 3}, {}, "tag must be a string"),
    ("echo", {"text": "x"}, {"cwd": 5}, "inputs must be an object and cwd a string"),
    ("echo", {"text": "x"}, {"in_dir": 5}, "in_dir and work must be strings"),
    ("echo", {"text": "x"}, {"work": []}, "in_dir and work must be strings"),
])
def test_bad_input_types_are_32602(plugin, command, inputs, extra, message):
    answer = plugin.answer(plugin.start_with(command, inputs, **extra))
    assert answer["error"] == {"code": -32602, "message": message}


def test_params_that_are_not_an_object_are_32602(plugin):
    plugin.send(id=4, method="invoke", params=[1])
    assert plugin.answer(4)["error"] == {"code": -32602, "message": "params must be an object"}


def test_a_failure_without_a_code_logs_an_error_and_exits_1(plugin):
    answer = plugin.invoke("fail", message="no good")
    assert answer["result"] == {"exit_code": 1}
    assert [m["params"] for m in plugin.seen] == [{"level": "error", "message": "no good"}]


def test_a_failure_with_a_code_is_a_json_rpc_error(plugin):
    assert plugin.invoke("fail", message="bad", code=-32602)["error"] == {"code": -32602, "message": "bad"}


def test_any_other_exception_is_32603_and_the_plugin_keeps_serving(plugin):
    answer = plugin.invoke("boom")
    assert answer["error"] == {"code": -32603, "message": "internal error: kaboom"}
    assert logs(plugin) == ["sample failed: RuntimeError('kaboom')"]
    assert plugin.invoke("echo", text="again")["result"]["exit_code"] == 0


def test_the_context_has_an_absolute_cwd_and_the_folders(plugin):
    answer = plugin.answer(plugin.start_with("where", {}, cwd=".", in_dir="/i", work="/w"))
    out = answer["result"]["outputs"]
    assert Path(out["cwd"]).is_absolute()
    assert (out["in_dir"], out["work"]) == ("/i", "/w")


def test_bar_uses_the_plugins_id_and_label_and_table_is_sent(plugin):
    plugin.invoke("show")
    assert [(m["method"], m["params"]) for m in plugin.seen] == [
        ("bar", {"id": "sample", "label": "sampling", "percent": 5, "done": False}),
        ("table", {"id": "t", "title": "title", "columns": ["a", "b"], "rows": [[1, 2]]}),
    ]


def test_a_cancel_during_an_invoke_answers_32800_and_the_next_invoke_works(plugin):
    req = plugin.start("hold")
    plugin.until(lambda m: m.get("method") == "log")
    plugin.send(method="cancel", params={})
    assert plugin.answer(req)["error"]["code"] == -32800
    assert plugin.invoke("echo", text="x")["result"]["exit_code"] == 0


def test_a_cancel_with_nothing_running_is_ignored(plugin):
    plugin.send(method="cancel", params={})
    assert plugin.invoke("echo", text="x")["result"]["exit_code"] == 0


def test_closing_stdin_during_an_invoke_cancels_it(plugin):
    req = plugin.start("hold")
    plugin.until(lambda m: m.get("method") == "log")
    assert plugin.proc.stdin
    plugin.proc.stdin.close()
    assert plugin.answer(req)["error"]["code"] == -32800
    assert plugin.proc.wait(10) == 0


def test_a_host_answer_reaches_the_asking_command_by_its_id(plugin):
    req = plugin.start("ask", q="name?")
    prompt = plugin.until(lambda m: m.get("method") == "prompt")
    assert prompt["params"] == {"q": "name?"}
    plugin.send(id="x99", result={"wrong": True})  # nobody asked for this one: dropped
    plugin.send(id=prompt["id"], result={"name": "dock"})
    assert plugin.answer(req)["result"]["outputs"] == {"reply": {"name": "dock"}}


def test_each_request_gets_its_own_id(plugin):
    ids = []
    for _ in range(2):
        req = plugin.start("ask")
        prompt = plugin.until(lambda m: m.get("method") == "prompt")
        ids.append(prompt["id"])
        plugin.send(id=prompt["id"], result={})
        plugin.answer(req)
    assert ids[0] != ids[1]


def test_an_error_answer_from_the_host_fails_the_command_with_its_message(plugin):
    req = plugin.start("ask")
    prompt = plugin.until(lambda m: m.get("method") == "prompt")
    plugin.send(id=prompt["id"], error={"code": -1, "message": "denied"})
    assert plugin.answer(req)["result"] == {"exit_code": 1}
    assert logs(plugin) == ["denied"]


def test_stdin_closing_while_asking_gives_eof_not_a_hang(plugin):
    req = plugin.start("ask")
    plugin.until(lambda m: m.get("method") == "prompt")
    assert plugin.proc.stdin
    plugin.proc.stdin.close()
    assert plugin.answer(req)["result"]["outputs"] == {"reply": {"eof": True}}


def test_importing_rpc_does_not_load_net_or_files():
    code = (f"import sys; sys.path.insert(0, {str(KIT)!r}); import dock_plugin.rpc; "
            "bad = [m for m in ('dock_plugin.net', 'dock_plugin.files', 'dock_plugin.env') if m in sys.modules]; "
            "sys.exit(','.join(bad) or 0)")
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_the_kit_imports_only_the_standard_library():
    allowed = set(sys.stdlib_module_names)
    for path in (KIT / "dock_plugin").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            assert set(roots) <= allowed, f"{path.name} imports {roots}"
