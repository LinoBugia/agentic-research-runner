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
