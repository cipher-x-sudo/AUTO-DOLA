from app.services.dola import DolaSubmissionError
from app.worker import BROWSER_SUBMIT_PARALLEL, effective_video_parallel, resolve_effective_dola_mode, should_fallback_to_browser


def test_browser_fallback_for_high_demand() -> None:
    assert should_fallback_to_browser(DolaSubmissionError("high demand", {"error_code": 710022002})) is True


def test_browser_fallback_for_country_restriction() -> None:
    assert should_fallback_to_browser(DolaSubmissionError("restricted", {"error_code": 710022017})) is True


def test_browser_fallback_for_common_invalid_param() -> None:
    assert should_fallback_to_browser(DolaSubmissionError("common invalid param", {})) is True


def test_browser_fallback_ignores_other_errors() -> None:
    assert should_fallback_to_browser(DolaSubmissionError("temporary network issue", {"error_code": 500})) is False


def test_effective_video_parallel_uses_requested_value() -> None:
    assert effective_video_parallel(5) == 5


def test_effective_video_parallel_clamps_to_bounds() -> None:
    assert effective_video_parallel(0) == 1
    assert effective_video_parallel("99") == 50
    assert effective_video_parallel("bad") == 1


def test_browser_submit_parallel_is_separate_from_video_parallel() -> None:
    assert BROWSER_SUBMIT_PARALLEL == 5


def test_disabled_direct_submit_forces_browser_for_all_modes() -> None:
    assert resolve_effective_dola_mode("direct", 10, False) == "browser"
    assert resolve_effective_dola_mode("hybrid", 10, False) == "browser"


def test_enabled_direct_submit_preserves_existing_routing() -> None:
    assert resolve_effective_dola_mode("direct", 10, True) == "direct"
    assert resolve_effective_dola_mode("hybrid", 10, True) == "hybrid"
    assert resolve_effective_dola_mode("hybrid", 15, True) == "browser"
