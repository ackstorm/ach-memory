"""The argument parser and the three subcommands' dispatch, with `proxy` stubbed out."""

import asyncio
import os

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


def test_main_mcp_without_url_serves_the_endpoint_the_proxy_resolves(monkeypatch):
    monkeypatch.delenv("ACH_MEMORY_URL", raising=False)
    seen = {}

    async def fake_serve(url):
        seen["url"] = url

    monkeypatch.setattr(cli.proxy, "serve", fake_serve)
    monkeypatch.setattr(cli.proxy, "endpoint", lambda: "https://hub.example/mcp/ach-memory")
    assert cli.main(["mcp"]) == 0
    assert seen == {"url": "https://hub.example/mcp/ach-memory"}


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


def test_main_hook_idle_nudge_prints_the_clock(capsys):
    assert cli.main(["hook", "idle-nudge"]) == 0
    out = capsys.readouterr().out
    assert "for over 30 minutes" in out
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

    assert cli.main(["context", "load"]) == 1
    assert capsys.readouterr().out == ""


def test_main_context_load_gives_up_at_its_deadline(monkeypatch, capsys):
    """A hung call must end inside the host's hook timeout, so the hook can say so."""
    async def hung_call_load_context(url, slug):
        await asyncio.sleep(60)

    monkeypatch.setattr(cli.proxy, "call_load_context", hung_call_load_context)
    monkeypatch.setattr(cli.proxy, "resolve_project_context", lambda: "acme-1")
    monkeypatch.setattr(cli, "_CONTEXT_LOAD_DEADLINE", 0.05)

    assert cli.main(["context", "load"]) == 1
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


def test_project_init_writes_the_marker_once(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.proxy, "_origin_url", lambda cwd: "")

    assert cli.main(["project", "init", "Design MD2"]) == 0
    assert (tmp_path / ".ach-memory").read_text() == 'project = "design-md2"\n'
    assert cli.proxy.resolve_project_context(str(tmp_path)) == "design-md2"

    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")
    assert cli.main(["project", "init", "other"]) == 1
    assert "already names" in capsys.readouterr().err
    assert not (tmp_path / "sub" / ".ach-memory").exists()


def test_project_init_refuses_a_folder_with_a_git_origin_or_a_bad_name(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.proxy, "_origin_url", lambda cwd: "git@github.com:acme/app.git")
    assert cli.main(["project", "init", "app"]) == 1
    assert "git origin" in capsys.readouterr().err

    monkeypatch.setattr(cli.proxy, "_origin_url", lambda cwd: "")
    assert cli.main(["project", "init", "!!"]) == 1
    assert not (tmp_path / ".ach-memory").exists()


def test_project_init_refuses_the_home_folder_and_a_marker_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.proxy, "_origin_url", lambda cwd: "")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["project", "init", "notes"]) == 1
    assert "home folder" in capsys.readouterr().err

    (tmp_path / "work" / ".ach-memory").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "work")
    assert cli.main(["project", "init", "notes"]) == 1
    assert not (tmp_path / ".ach-memory").exists()


def test_main_mcp_in_pwd_serves_from_the_folder_the_host_passed_down(monkeypatch, tmp_path):
    session, plugin = tmp_path / "session", tmp_path / "plugin"
    session.mkdir()
    plugin.mkdir()
    monkeypatch.chdir(plugin)
    monkeypatch.setenv("PWD", str(session))
    seen = []

    async def fake_serve(url):
        seen.append(os.getcwd())

    monkeypatch.setattr(cli.proxy, "serve", fake_serve)
    assert cli.main(["mcp", "--url", "http://x/mcp"]) == 0
    assert cli.main(["mcp", "--url", "http://x/mcp", "--in-pwd"]) == 0
    monkeypatch.setenv("PWD", str(tmp_path / "gone"))
    assert cli.main(["mcp", "--url", "http://x/mcp", "--in-pwd"]) == 0
    assert seen == [str(plugin), str(session), str(session)]


def _record_calls(monkeypatch, answer):
    calls = []

    async def fake_call_tool(url, name, arguments, slug):
        calls.append((name, arguments, slug))
        return answer

    monkeypatch.setattr(cli.proxy, "call_tool", fake_call_tool)
    monkeypatch.setattr(cli.proxy, "endpoint", lambda: "http://memory.test/mcp/")
    monkeypatch.setattr(cli.proxy, "resolve_project_context", lambda: "acme-1")
    return calls


def test_whoami_prints_subject_and_groups(monkeypatch, capsys):
    calls = _record_calls(monkeypatch, {"subject": "juan@acme.test", "groups": ["default", "dream"],
                                        "operator": False})

    assert cli.main(["whoami"]) == 0
    assert calls == [("whoami", {}, None)]
    assert capsys.readouterr().out == "juan@acme.test\ngroups: default, dream\n"


def test_share_without_arguments_lists_access(monkeypatch, capsys):
    calls = _record_calls(monkeypatch, {"users": ["juan@acme.test"], "groups": ["dream"]})

    assert cli.main(["share"]) == 0
    assert calls == [("list_access", {}, "acme-1")]
    assert capsys.readouterr().out == "user\tjuan@acme.test\ngroup\tdream\n"


def test_share_and_unshare_send_users_or_groups(monkeypatch, capsys):
    calls = _record_calls(monkeypatch, {"users": ["juan@acme.test"], "groups": []})

    assert cli.main(["share", "user", "joan@acme.test", "ana@acme.test"]) == 0
    assert cli.main(["unshare", "group", "dream", "--project", "other"]) == 0
    assert calls == [("share_project", {"users": ["joan@acme.test", "ana@acme.test"]}, "acme-1"),
                     ("unshare_project", {"groups": ["dream"]}, "other")]


def test_share_user_without_a_name_and_a_server_refusal_exit_nonzero(monkeypatch, capsys):
    _record_calls(monkeypatch, {})
    assert cli.main(["share", "user"]) == 1

    async def refuse(url, name, arguments, slug):
        raise RuntimeError("INVALID_REQUEST: you are not in run")

    monkeypatch.setattr(cli.proxy, "call_tool", refuse)
    assert cli.main(["share", "group", "run"]) == 1
    assert "you are not in run" in capsys.readouterr().err
