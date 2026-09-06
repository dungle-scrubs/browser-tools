"""Every curated action accepts the raw protocol target slot."""

import pytest

from browser_tools.cli import build_parser


@pytest.mark.parametrize(
    "verb",
    [
        "snapshot",
        "click",
        "fill",
        "wait-idle",
        "wait-stable",
        "detect",
        "frames list",
        "frames select example",
        "frames reset",
        "storage get",
        "screenshot",
    ],
)
def test_curated_actions_accept_target_and_url(verb):
    parser = build_parser()
    assert parser.parse_args([*verb.split(), "--target", "2"]).target == "2"
    assert parser.parse_args([*verb.split(), "--url", "example"]).url == "example"


@pytest.mark.parametrize(
    "verb",
    [
        "snapshot",
        "click --uid 1",
        "fill --uid 1 --text value",
        "wait-idle",
        "wait-stable",
        "detect",
        "frames list",
        "frames select example",
        "frames reset",
        "storage get",
        "screenshot",
        "tool get_text",
        "screencast record",
    ],
)
@pytest.mark.parametrize("selection", ["target", "url", "ambiguous"])
def test_curated_dispatch_uses_shared_target_resolution(
    verb, selection, tmp_path, monkeypatch, capsys
):
    from browser_tools import cli, curated, one_shot
    from browser_tools.lifecycle import LifecycleError

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, method, params=None, **kwargs):
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"type": "page", "targetId": "aaa", "url": "https://first.test"},
                        {"type": "page", "targetId": "bbb", "url": "https://second.test"},
                    ]
                }
            if method == "Target.attachToTarget":
                raise LifecycleError(f"selected target {params['targetId']}")
            return {}

    def runtime(port, target_id):
        raise LifecycleError(f"selected target {target_id}")

    monkeypatch.setattr(curated, "_resolve_port", lambda *args: 9222)
    monkeypatch.setattr(curated, "CDPRuntime", runtime)
    monkeypatch.setattr(one_shot, "CDPClient", Client)
    monkeypatch.setattr(one_shot, "get_ws_url", lambda **kwargs: "ws://test/browser")
    args = verb.split()
    if verb.startswith("screencast"):
        args += ["--dir", str(tmp_path / "frames")]
    if selection == "target":
        args += ["--target", "2"]
    elif selection == "url":
        args += ["--url", "second.test"]
    assert cli.main(args) == cli.EXIT_OPERATIONAL
    error = capsys.readouterr().err
    if selection == "ambiguous":
        assert "Multiple" in error or "Ambiguous" in error or "ambiguous" in error
    else:
        assert "selected target bbb" in error
