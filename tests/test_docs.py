"""Documentation contract: README, docs/, the shipped instructions and prompt, reference texts, tool descriptions."""
import asyncio
import importlib.util
import os
import re
import sys

import pytest

from conftest import ROOT, SRC, free_udp_port

from backseat_champions_mcp import __version__
from backseat_champions_mcp import instructions as I
from backseat_champions_mcp import reference as R

README = os.path.join(ROOT, "README.md")
DOCS = os.path.join(ROOT, "docs")
PKG = os.path.join(SRC, "backseat_champions_mcp")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def doc_texts():
    """README, every file under docs/, the instructions and the reference module: name -> text."""
    out = {"README.md": read(README)}
    for name in sorted(os.listdir(DOCS)):
        out["docs/" + name] = read(os.path.join(DOCS, name))
    out["instructions.INSTRUCTIONS"] = I.INSTRUCTIONS
    out["instructions.race_engineer_prompt"] = I.race_engineer_prompt("win")
    out["reference.py"] = read(os.path.join(PKG, "reference.py"))
    return out


def tool_texts():
    """Every registered tool's description and parameter descriptions, prompts and resources, in process."""
    from backseat_champions_mcp.client import Config
    from backseat_champions_mcp.server import build_server

    async def collect():
        server = build_server(Config(allow_drive=True, results_port=0, results_dir=os.devnull))
        out = {}
        for t in await server.list_tools():
            parts = [t.description or ""]
            for name, schema in ((t.input_schema or {}).get("properties") or {}).items():
                parts.append("%s: %s" % (name, schema.get("description", "")))
            out["tool " + t.name] = "\n".join(parts)
        for p in await server.list_prompts():
            out["prompt " + p.name] = p.description or ""
        return out
    return asyncio.run(collect())


# ------------------------------------------------------------------------------------------------ version (L8)

def test_version_matches_pyproject():
    m = re.search(r'^version\s*=\s*"([^"]+)"', read(os.path.join(ROOT, "pyproject.toml")), re.M)
    assert m and m.group(1) == __version__


# ------------------------------------------------------------------------------------------------ instructions (L9, X14)

def check_instructions(text):
    lines = text.strip("\n").splitlines()
    assert len(lines) <= 20, len(lines)
    assert "55" in text
    assert "since_seq" in text and "last_seq" in text
    assert re.search(r"card_offer first", text, re.I)
    assert "wait_for_event" not in text
    assert "pick_card(index, seq)" in text
    assert "resent" in text
    assert "never sends a" in text and "gameplay command on its own" in text.replace("\n", " ")
    for value in re.findall(r"timeout_s\s*=\s*(\d+(?:\.\d+)?)", text):
        assert float(value) <= 60
    flat = " ".join(text.split())
    # V-D6: step 1 also stops on an unbound telemetry listener and relays the hint.
    step1 = flat.split("1. ", 1)[1].split(" 2. ", 1)[0]
    assert "telemetry.bound" in step1 and "hint" in step1 and "stop" in step1
    # V-D6: retry with the same since_seq only when the client cut the call / transport failed; ok:false -> hint.
    assert "ok:false" in flat.replace(" ", "") and "follow its hint" in flat
    assert "never retry in a tight loop" in flat
    assert "If a call fails or your client cuts it" not in flat
    # V-D7: puncture pits (can_pit, race not nearly over); performance_drop checks get_car_status first.
    assert "signal puncture" in flat and "performance_drop" in flat
    assert re.search(r"performance_drop.{0,80}get_car_status", flat)


def test_instructions_and_prompt_agree_on_car_damaged():
    for text in (I.INSTRUCTIONS, I.race_engineer_prompt(None)):
        flat = " ".join(text.split())
        assert re.search(r"puncture.{0,120}can_pit.{0,60}not nearly over.{0,20}set_pit\(true\)|"
                         r"puncture.{0,120}set_pit\(true\)", flat)
        assert re.search(r"performance_drop.{0,120}get_car_status\(car=\"me\"\) first.{0,30}pit only on a clear "
                         r"failure", flat)


def test_prompt_no_tight_loop_retry():
    flat = " ".join(I.race_engineer_prompt(None).split())
    assert "ok: false is not that case" in flat and "never retry in a tight loop" in flat


def test_instructions_text():
    check_instructions(I.INSTRUCTIONS)


def test_initialize_instructions_over_stdio():
    """The server delivers INSTRUCTIONS in the MCP initialize result, with the package version."""
    from mcp import ClientSession, StdioServerParameters, stdio_client

    env = dict(os.environ)
    env.update({"PYTHONPATH": SRC, "BC_TELEMETRY_PORT": str(free_udp_port()),
                "BC_COMMAND_PORT": str(free_udp_port()), "BC_COMMAND_TIMEOUT": "0.2"})
    params = StdioServerParameters(command=sys.executable, args=["-m", "backseat_champions_mcp"], env=env, cwd=ROOT)

    async def main():
        with open(os.devnull, "w") as errlog:
            async with stdio_client(params, errlog=errlog) as (r, w):
                async with ClientSession(r, w) as session:
                    init = await asyncio.wait_for(session.initialize(), 20)
                    prompts = await session.list_prompts()
                    return init, prompts
    init, prompts = asyncio.run(main())
    assert init.instructions == I.INSTRUCTIONS
    check_instructions(init.instructions)
    assert init.server_info.version == __version__
    names = {p.name: p for p in prompts.prompts}
    assert "race_engineer" in names


def test_race_engineer_prompt():
    text = " ".join(I.race_engineer_prompt(None).split())
    assert "wait_for_event" not in text
    assert "wait_for_trigger(timeout_s=55, since_seq=last)" in text
    assert "pick_card(index, seq)" in text and "hand_changed" in text
    assert "resent" in text and "open_decisions" in text and "last_seq" in text
    assert "card_offer before anything else" in text
    assert "## The loop" in I.race_engineer_prompt(None)
    for switch in ("Driver Data", "Player Data", "Decision Events", "Player Input", "AI Observations", "Driver Input"):
        assert switch in text
    assert "Optional (do not stop for them)" in text
    assert 'car="me", "ahead" or "behind"' in text
    assert "cannot start races" in text
    assert "{goal}" not in text and "The user's goal" not in text
    with_goal = I.race_engineer_prompt("finish in the top 3")
    assert "finish in the top 3" in with_goal and "{" not in with_goal.replace("{ok", "")
    for value in re.findall(r"timeout_s\s*=\s*(\d+(?:\.\d+)?)", text):
        assert float(value) <= 60


# ------------------------------------------------------------------------------------------------ README (L9, U1)

def test_readme_starts_with_install_and_connect():
    lines = read(README).splitlines()
    assert lines[0].startswith("# ")
    first_h2 = next(i for i, line in enumerate(lines) if line.startswith("## "))
    assert lines[first_h2] == "## Install & connect"
    intro = [line for line in lines[1:first_h2] if line.strip()]
    assert len(intro) <= 3, intro


def test_readme_install_contents():
    text = read(README)
    install = text.split("## Install & connect", 1)[1].split("\n## ", 1)[0]
    for needle in ("3.10", "uv", "Driver Data", "Player Data", "Decision Events", "Player Input", "AI Observations",
                   "BC_ALLOW_DRIVE", "127.0.0.1:29770", "BC_TELEMETRY_PORT", "command -v uvx",
                   "/path/to/backseat_champions_ai-server", "$HOME", "uv tool install .",
                   "command -v backseat-champions-mcp", "uv tool install --reinstall .",
                   'claude mcp add --transport stdio backseat-champions -- "$(command -v backseat-champions-mcp)"',
                   'uvx --from "$PWD" backseat-champions-mcp --version', "uv 0.11.32",
                   "uv cache clean backseat-champions-mcp && uv cache prune", "beta", "Settings > Developer > Edit Config",
                   "python3-venv", "https://astral.sh/uv/install.sh", "two Claude\nCode sessions",
                   "get_api_status", "server_version", "telemetry.bound",
                   "telemetry.receiving", "cannot start races", "80 s", "55"):
        assert needle in install, needle
    assert "## Does the AI need instructions?" in text
    # V-D1: "uv cache clean <pkg>" alone does not refresh the uvx build (uv 0.11.32); it must never be the
    # whole update method.
    flat = " ".join(text.split())
    for m in re.finditer(r"uv cache clean backseat-champions-mcp(?! && uv cache prune)", flat):
        context = flat[m.start():m.start() + 200]
        assert "alone" in context, context
    assert "Most MCP clients pass" in text and "race_engineer" in text
    assert "### Other MCP clients" in text and "BC_WAIT_MAX_S" in text.split("### Other MCP clients", 1)[1]


def test_readme_trigger_model_and_config():
    text = read(README)
    for needle in ("since_seq", "last_seq", "resent: true", "open_decisions", "skipped", "20 s",
                   "card_offer,car_damaged,tires_wont_last,tire_screen", "floor_no_forecast",
                   "| `BC_WAIT_MAX_S` | `55` |", "hard cap of 60", "BC_RESULTS_KEEP", "include_steam_ids",
                   "isError", "answered **200**", "0600", "BC_UPSTREAM_GAMEAPI", "BC_REMIND_S",
                   "10 s of the connection being served", "at\n  most 120 s", "when the\n  receiver starts and after each saved upload",
                   "resolved against the server's working directory"):
        assert needle in text, needle
    assert "still_open" not in text
    assert "wait_for_event(codes=[\"TIRE\"])" not in text


# ------------------------------------------------------------------------------------------------ timeouts (L4, X14)

def test_no_timeout_over_60_in_docs():
    pattern = re.compile(r"timeout_s\W{0,6}(\d+(?:\.\d+)?)")
    for name, text in doc_texts().items():
        for value in pattern.findall(text):
            assert float(value) <= 60, (name, value)


# ------------------------------------------------------------------------------------------------ D4

D4_PHRASES = ("never acts on its own", "never sends a command on its own", "never sends a command by itself")


def test_d4_phrase_absent_in_docs():
    for name, text in doc_texts().items():
        flat = " ".join(text.split()).lower()
        for phrase in D4_PHRASES:
            assert phrase not in flat, (name, phrase)
    assert "never sends a gameplay command on its own" in " ".join(read(README).split())


def test_d4_phrase_absent_in_tools():
    for name, text in tool_texts().items():
        flat = " ".join(text.split()).lower()
        for phrase in D4_PHRASES:
            assert phrase not in flat, (name, phrase)


# ------------------------------------------------------------------------------------------------ reference (C8, C9, D15)

def test_invalid_packet_mentions_token():
    explanation, _ = R.explain_error("invalid_packet")
    assert "from another machine only answered when it carries the token" in explanation


def test_boost_withholding_marked_inferred():
    api = read(os.path.join(DOCS, "API_REFERENCE.md"))
    boost = api.split("#### 8 `boost`", 1)[1].split("#### 9", 1)[0]
    assert "[inferred]" in boost and "withholding" in boost
    assert "so it does not\n  trigger the achievement" not in boost
    assert "inferred" in R.reference("rules")["boost_withholding"]


def test_api_reference_cleanups():
    api = read(os.path.join(DOCS, "API_REFERENCE.md"))
    assert "scratch/" not in api and "decode_testdata.py" not in api
    section9 = api.split("## 9.", 1)[1].split("## 10.", 1)[0]
    assert "TOOLS.md" in section9
    for gone in ("get_my_car", "get_hand", "poll_events", "get_enums", "get_strategy_features"):
        assert gone not in section9
    section44 = api.split("### 4.4", 1)[1].split("## 5.", 1)[0]
    assert "[inferred]" in section44.split("####", 1)[0]
    assert "[observed 0.88.2]" in api and "fixed for the session" not in api


# README gives no description for these reasons (its table cell is empty and no rule describes them).
README_SILENT_REASONS = ("unknown_command", "replay_playing", "no_points", "invalid_index", "cards_unavailable",
                         "card_banned", "invalid_compound", "tire_select_closed", "internal_error")


def test_reference_marks_inference():
    for reason in README_SILENT_REASONS:
        explanation, _ = R.explain_error(reason)
        assert "inferred" in explanation or "only the name" in explanation, reason


def test_reference_has_every_reason_the_code_uses():
    code = ""
    for name in os.listdir(PKG):
        if name.endswith(".py"):
            code += read(os.path.join(PKG, name))
    used = set(re.findall(r'failure\(\s*"([a-z_]+)"', code))
    used |= set(re.findall(r'"error_reason"\s*:\s*"([a-z_]+)"', code))
    used |= set(re.findall(r'ResultsError\(\s*"([a-z_]+)"', code))
    used |= {"protocol_error", "server_error", "stored_file_unreadable", "unsupported_format", "hand_changed",
             "bad_argument", "no_data", "switch_off", "not_in_session", "telemetry_listener_down", "no_reply",
             "game_unreachable", "send_failed", "drive_disabled"}
    known = set(R.ERROR_INFO) | set(R.LOCAL_ERROR_INFO)
    assert used <= known, sorted(used - known)
    assert "outcome is unknown" in R.OUTCOME_UNKNOWN_HINT


def test_reference_triggers_topic_is_x2_model():
    t = R.reference("triggers")
    text = str(t)
    assert "still_open" not in text and "every call until answered" not in text
    for needle in ("since_seq", "last_seq", "resent", "open_decisions", "20 s", "55", "skipped"):
        assert needle in text, needle
    assert t["statuses"] == ["triggered", "session_ended", "timeout"]
    assert any(x["type"] == "tire_screen" and "on by default" in x["fires_on"] for x in t["triggers"])


def test_reference_observed_facts():
    obs = R.reference("observed")
    text = " ".join(str(v) for v in obs.values())
    assert "0.88.2" in obs["game_version"]
    assert "pauses" in obs["pause_during_hand"]
    assert "re-order" in obs["car_indices_reorder"] and "README" in obs["car_indices_reorder"]
    assert "pit lane" in obs["cards_unavailable_in_pits"]
    assert "80 s" in obs["client_call_limit"]
    assert "observed" in R.ERROR_INFO["cards_unavailable"][1].lower()
    assert text


# ------------------------------------------------------------------------------------------------ V-D9

def test_no_doc_claims_resource_updated_notifications():
    """The server sends no notifications/resources/updated or notifications/message (V-D9)."""
    for name, text in doc_texts().items():
        flat = " ".join(text.split())
        for m in re.finditer(r"notifications/resources/updated|notifications/message", flat):
            context = flat[max(0, m.start() - 120):m.end()]
            assert re.search(r"\bno\b|never|not sen", context), (name, context)
    flat = " ".join(read(README).split())
    assert "sends no resource-updated or log notifications" in flat
    assert "BC_PROGRESS_INTERVAL_S` | `10` |" in flat and "<= 60" in flat
    assert "0.05 to 30 s" in flat and "0.05 to 10 s" in flat


# ------------------------------------------------------------------------------------------------ T8

def test_no_fixed_for_the_session_claim_in_tools():
    for name, text in tool_texts().items():
        flat = " ".join(text.split()).lower()
        assert "fixed for the session" not in flat, name
        assert "stays the same for the whole session" not in flat, name
        assert "wait_for_event([\"tire\"])" not in flat, name


# ------------------------------------------------------------------------------------------------ U1

# Built from pieces so this file does not match itself.
FORBIDDEN = tuple("/" + d + "/" for d in ("home", "Users", "mnt", "tmp")) + ("C:" + "\\",)


def test_no_machine_specific_paths():
    targets = [README, os.path.join(ROOT, "pyproject.toml")]
    for top in ("docs", "tests", "src", "scripts"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, top)):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".pytest_cache")]
            for name in filenames:
                if name.endswith((".pyc", ".bin")):
                    continue
                targets.append(os.path.join(dirpath, name))
    hits = []
    for path in targets:
        with open(path, "rb") as f:
            data = f.read()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for needle in FORBIDDEN:
            if needle in text:
                hits.append((os.path.relpath(path, ROOT), needle))
    assert not hits, hits


# ------------------------------------------------------------------------------------------------ D16

def test_tools_md_current():
    spec = importlib.util.spec_from_file_location("gen_tools_doc", os.path.join(ROOT, "scripts", "gen_tools_doc.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = asyncio.run(module.render())
    assert read(os.path.join(DOCS, "TOOLS.md")) == expected, "run: python3 scripts/gen_tools_doc.py"
