import json
from pathlib import Path

import httpx
import pytest

from pe_review_agent.config import GerritSettings
from pe_review_agent.gerrit.client import GerritRestClient, SupersededRevisionError
from pe_review_agent.gerrit.votes import parse_vote_observation
from pe_review_agent.retry import PermanentError, TransientError

SHA = "a" * 40


def detail(sha=SHA):
    return {
        "project": "team/fw",
        "_number": 12,
        "status": "NEW",
        "current_revision": sha,
        "revisions": {sha: {"_number": 2, "ref": "refs/changes/12/12/2"}},
        "labels": {
            "Code-Review": {
                "all": [{"_account_id": 7, "value": 1}, {"_account_id": 99, "value": -2}]
            }
        },
        "permitted_labels": {"Code-Review": [" 0", "+1"]},
    }


def message(**updates):
    return {
        "_revision_number": 2,
        "author": {"_account_id": 7},
        "tag": "unique-vote",
        "message": "Patch Set 2: Code-Review+1\n\nresult [vote:123]",
        **updates,
    }


def observe(data, messages):
    return parse_vote_observation(
        data,
        messages,
        account_id=7,
        patchset_number=2,
        tag="unique-vote",
        marker="result [vote:123]",
        target=1,
    )


def test_vote_recovery_only_matches_exact_bot_patchset_and_marker():
    result = observe(detail(), [message()])
    assert result.marker_found and result.can_vote and result.current_value == 1
    for bad in (
        {"author": {"_account_id": 99}},
        {"_revision_number": 1},
        {"tag": "comment-tag"},
        {"message": "not our marker"},
    ):
        assert not observe(detail(), [message(**bad)]).marker_found


@pytest.mark.parametrize("bad", [None, [], {"Code-Review": 1}, {"Code-Review": {"all": 1}}])
def test_malformed_label_envelope_never_looks_like_neutral(bad):
    data = detail()
    data["labels"] = bad
    with pytest.raises(TransientError):
        observe(data, [])


@pytest.mark.asyncio
async def test_authenticated_identity_and_current_ps_label_preflight_are_read_only():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/accounts/self"):
            body = {"_account_id": 7}
        elif request.url.path.endswith("/detail"):
            assert request.url.params.get_list("o") == ["CURRENT_REVISION", "DETAILED_LABELS"]
            body = detail()
        else:
            body = [message()]
        return httpx.Response(200, text=")]}'\n" + json.dumps(body))

    settings = GerritSettings(
        ssh_host="gerrit",
        ssh_user="bot",
        ssh_key_path=Path("key"),
        rest_url="https://gerrit/context",
        projects=["team/fw"],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GerritRestClient(settings, client=http)
        assert await client.authenticated_account_id() == 7
        result = await client.code_review_vote_observation(
            project="team/fw",
            change_number=12,
            revision_sha=SHA,
            patchset_number=2,
            account_id=7,
            tag="unique-vote",
            marker="result [vote:123]",
            target=1,
        )
    assert result.marker_found
    assert all(req.method == "GET" for req in requests)
    assert "team%2Ffw~12" in requests[1].url.raw_path.decode()


@pytest.mark.asyncio
async def test_vote_preflight_rejects_newer_revision_before_reading_messages():
    def handler(request):
        assert request.url.path.endswith("/detail")
        return httpx.Response(200, json=detail("b" * 40))

    settings = GerritSettings(
        ssh_host="gerrit",
        ssh_user="bot",
        ssh_key_path=Path("key"),
        rest_url="https://gerrit",
        projects=["team/fw"],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SupersededRevisionError):
            await GerritRestClient(settings, client=http).code_review_vote_observation(
                project="team/fw",
                change_number=12,
                revision_sha=SHA,
                patchset_number=2,
                account_id=7,
                tag="tag",
                marker="marker",
                target=1,
            )


@pytest.mark.asyncio
async def test_anonymous_or_malformed_identity_cannot_vote():
    settings = GerritSettings(
        ssh_host="gerrit",
        ssh_user="bot",
        ssh_key_path=Path("key"),
        rest_url="https://gerrit",
        projects=["team/fw"],
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"username": "bot"}))
    ) as http:
        with pytest.raises(PermanentError):
            await GerritRestClient(settings, client=http).authenticated_account_id()
