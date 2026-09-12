# A literal, not importlib.metadata: the image never installs this
# distribution (the Dockerfile exports the lock with --no-emit-project and
# puts src/ on PYTHONPATH), so metadata.version("ach-memory") raises there.
# release-bump rewrites it with the rest of the release metadata and
# tests/test_release_flow.py holds it equal to pyproject.toml.
__version__ = "0.7.5"
