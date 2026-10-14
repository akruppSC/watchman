from typer.testing import CliRunner

from watchman_review import __version__
from watchman_review.cli import app

runner = CliRunner()


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__
