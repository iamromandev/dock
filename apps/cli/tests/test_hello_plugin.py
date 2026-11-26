"""The hello plugin's invoke answer, driven over its stdio protocol (no container)."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

HELLO = Path(__file__).resolve().parent.parent.parent.parent / "plugins" / "hello" / "main.py"


def invoke(command: str, inputs: dict) -> dict:
    lines = [
        {"id": 1, "method": "initialize", "params": {}},
        {"id": 2, "method": "invoke", "params": {"command": command, "args": [], "cwd": "/", "inputs": inputs}},
        {"id": 3, "method": "shutdown", "params": {}},
    ]
    stdin = "".join(json.dumps({"jsonrpc": "2.0", **m}) + "\n" for m in lines)
    r = subprocess.run([sys.executable, str(HELLO)], input=stdin, capture_output=True, text=True, timeout=30, check=False)
    answers = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
    return next(a for a in answers if a.get("id") == 2)


class HelloOutputs(unittest.TestCase):
    def test_hello_answers_with_the_greeting_it_printed(self):
        answer = invoke("hello", {"name": "Ann"})
        self.assertEqual(answer["result"], {"exit_code": 0, "outputs": {"greeting": "hello, Ann!"}})

    def test_count_has_no_outputs(self):
        self.assertEqual(invoke("count", {"n": 1})["result"], {"exit_code": 0})


if __name__ == "__main__":
    unittest.main()
