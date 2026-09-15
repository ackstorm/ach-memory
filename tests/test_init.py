from memory import init


def test_plugins_root_points_at_a_tree_with_every_host():
    root = init._plugins_root()
    assert (root / "shared" / "activation.txt").is_file()
    assert (root / "claude-code" / ".claude-plugin" / "plugin.json").is_file()
