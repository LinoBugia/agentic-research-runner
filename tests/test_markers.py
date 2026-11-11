from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def test_live_tests_are_deselected_by_default(pytester: pytest.Pytester) -> None:
    pytester.makepyprojecttoml(PYPROJECT.read_text(encoding="utf-8"))
    pytester.makepyfile(
        test_sample="""
        import pytest

        @pytest.mark.live
        def test_live():
            raise AssertionError("live test must not run by default")

        @pytest.mark.macos
        def test_macos():
            pass
        """
    )
    result = pytester.runpytest("test_sample.py", "--strict-markers")
    result.assert_outcomes(passed=1, deselected=1)


CONFTEST = Path(__file__).with_name("conftest.py")


@pytest.mark.parametrize(
    ("platform", "outcome"), [("linux", {"skipped": 1}), ("darwin", {"passed": 1})]
)
def test_the_macos_marker_skips_by_itself_on_other_systems(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, platform: str, outcome: dict
) -> None:
    """W4-01.3: a test marked only with `macos` (no `skipif`) is skipped off macOS by the hook
    in `conftest.py`, with a German reason; on macOS it runs (counter-check)."""
    pytester.makepyprojecttoml(PYPROJECT.read_text(encoding="utf-8"))
    pytester.makeconftest(CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(
        test_sample="""
        import pytest

        @pytest.mark.macos
        def test_macos():
            pass
        """
    )
    monkeypatch.setattr("sys.platform", platform)
    result = pytester.runpytest_inprocess("test_sample.py", "-rs", "--strict-markers")
    result.assert_outcomes(**outcome)
    if platform != "darwin":
        result.stdout.fnmatch_lines(["*nur macOS*"])
