from pe_review_agent.admin.web import _gerrit_change_url


def test_gerrit_change_url_preserves_context_and_strips_rest_auth_suffix() -> None:
    assert _gerrit_change_url(
        "https://gerrit.example/gerrit/a/",
        "sw_product/dmc_fw/dmc-solution",
        915231,
    ) == (
        "https://gerrit.example/gerrit/c/sw_product/dmc_fw/dmc-solution/+/915231"
    )


def test_gerrit_change_url_encodes_project_path_components() -> None:
    assert _gerrit_change_url("https://gerrit.example", "team/fw core", 12) == (
        "https://gerrit.example/c/team/fw%20core/+/12"
    )


def test_gerrit_change_url_rejects_non_http_or_relative_rest_urls() -> None:
    assert _gerrit_change_url("javascript:alert(1)", "team/fw", 12) is None
    assert _gerrit_change_url("gerrit.internal", "team/fw", 12) is None
