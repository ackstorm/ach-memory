"""The argument parser and the three subcommands' dispatch, with `proxy` stubbed out."""

from memory import cli


def test_parser_mcp_defaults_url_from_env(monkeypatch):
    monkeypatch.setenv("ACH_MEMORY_URL", "http://env/mcp")
    args = cli._parser().parse_args(["mcp"])
    assert args.url == "http://env/mcp"


def test_parser_mcp_url_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("ACH_MEMORY_URL", "http://env/mcp")
    args = cli._parser().parse_args(["mcp", "--url", "http://flag/mcp"])
    assert args.url == "http://flag/mcp"


def test_parser_context_load_project_defaults_to_none():
    args = cli._parser().parse_args(["context", "load"])
    assert args.command == "context"
    assert args.context_command == "load"
    assert args.project is None


def test_parser_hook_pre_compact():
    args = cli._parser().parse_args(["hook", "pre-compact"])
    assert args.command == "hook"
    assert args.hook_command == "pre-compact"


def test_main_with_no_command_exits_nonzero(capsys):
    assert cli.main([]) != 0


def test_main_mcp_without_url_fails_without_starting_the_proxy(monkeypatch, capsys):
    monkeypatch.delenv("ACH_MEMORY_URL", raising=False)
    called = False

    async def fake_serve(url):
        nonlocal called
        called = True

    monkeypatch.setattr(cli.proxy, "serve", fake_serve)
    assert cli.main(["mcp"]) == 1
    assert not called
    assert "url" in capsys.readouterr().err.lower()


def test_main_mcp_runs_the_proxy_with_the_resolved_url(monkeypatch):
    seen = {}

    async def fake_serve(url):
        seen["url"] = url

    monkeypatch.setattr(cli.proxy, "serve", fake_serve)
    assert cli.main(["mcp", "--url", "http://x/mcp"]) == 0
    assert seen == {"url": "http://x/mcp"}


def test_main_hook_pre_compact_prints_the_nudge(capsys):
    assert cli.main(["hook", "pre-compact"]) == 0
    out = capsys.readouterr().out
    assert "retain any durable decision" in out


def test_main_hook_retain_nudge_prints_the_bar(capsys):
    assert cli.main(["hook", "retain-nudge"]) == 0
    out = capsys.readouterr().out
    assert "Retain ONLY what a future session would need" in out
    # stop.sh embeds this text in JSON with printf; no escaping happens there.
    assert '"' not in out and "\\" not in out


def test_main_context_load_prints_the_text(monkeypatch, capsys):
    async def fake_call_load_context(url, slug):
        return "standing context"

    monkeypatch.setattr(cli.proxy, "call_load_context", fake_call_load_context)
    monkeypatch.setattr(cli.proxy, "resolve_project_context", lambda: "acme-1")

    assert cli.main(["context", "load"]) == 0
    assert capsys.readouterr().out.strip() == "standing context"


def test_main_context_load_names_the_missing_project(monkeypatch, capsys):
    async def fake_call_load_context(url, slug):
        return "standing context"

    monkeypatch.setattr(cli.proxy, "call_load_context", fake_call_load_context)
    monkeypatch.setattr(cli.proxy, "resolve_project_context", lambda: None)

    assert cli.main(["context", "load"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("standing context\n")
    assert "No project context" in out


def test_main_context_load_fails_open_and_prints_nothing(monkeypatch, capsys):
    async def fake_call_load_context(url, slug):
        raise RuntimeError("remote is down")

    monkeypatch.setattr(cli.proxy, "call_load_context", fake_call_load_context)
    monkeypatch.setattr(cli.proxy, "resolve_project_context", lambda: None)

    assert cli.main(["context", "load"]) == 0
    assert capsys.readouterr().out == ""


def test_main_context_load_project_flag_overrides_resolution(monkeypatch):
    seen = {}

    async def fake_call_load_context(url, slug):
        seen["slug"] = slug
        return ""

    monkeypatch.setattr(cli.proxy, "call_load_context", fake_call_load_context)
    monkeypatch.setattr(cli.proxy, "resolve_project_context", lambda: "from-git")

    cli.main(["context", "load", "--project", "explicit-slug"])
    assert seen["slug"] == "explicit-slug"
