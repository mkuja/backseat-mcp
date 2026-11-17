import importlib.util
import os
import socket
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
DATA = os.path.join(HERE, "data")
UPSTREAM = os.environ.get("BC_UPSTREAM_GAMEAPI", "/mnt/user-data/uploads/GameAPI")

for path in (SRC, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def load_bin(name):
    with open(os.path.join(DATA, name + ".bin"), "rb") as f:
        return f.read()


@pytest.fixture(scope="session")
def bc_api():
    """The game's reference codec, loaded by path; tests that need it skip when it is absent."""
    path = os.path.join(UPSTREAM, "bc_api.py")
    if not os.path.exists(path):
        pytest.skip("reference bc_api.py not found at %s" % path)
    spec = importlib.util.spec_from_file_location("bc_api_reference", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
