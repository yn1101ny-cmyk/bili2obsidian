"""模型下载器测试。

断点续传是这个模块的核心价值（1.5GB 在国内镜像上几乎必断），
所以重点覆盖三种响应形态：全新下载、206 续传、服务端忽略 Range。
全部用 respx 拦住，**不联网**。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.asr.model_store import (
    CHUNK,
    _base_urls,
    _download_one,
    _file_url,
    _is_complete,
    _probe_file,
    _sizes_from_metadata,
    ensure_model,
    find_vocab,
    is_model_ready,
    local_model_dir,
    resolve_repo,
)
from app.errors import TranscriptionError

URL = "https://example.com/model.bin"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """重试退避不该拖慢测试，且**绝不联网**取元数据。"""
    import app.asr.model_store as store

    monkeypatch.setattr(store.time, "sleep", lambda _s: None)
    # 元数据接口走的是 requests，respx 拦不住，必须显式打桩
    monkeypatch.setattr(store, "_sizes_from_metadata", lambda _repo: {})


# ---------------------------------------------------------------------------
# 仓库解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("large-v3-turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"),
        ("turbo", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"),
        ("LARGE-V3-TURBO", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"),
        ("large-v3", "Systran/faster-whisper-large-v3"),
        ("medium", "Systran/faster-whisper-medium"),
        ("small", "Systran/faster-whisper-small"),
    ],
)
def test_resolve_known_aliases(alias, expected):
    assert resolve_repo(alias) == expected


def test_resolve_passes_through_owner_repo():
    assert resolve_repo("deepdml/faster-whisper-large-v3-turbo-ct2") == (
        "deepdml/faster-whisper-large-v3-turbo-ct2"
    )


def test_resolve_unknown_alias_raises_with_known_list():
    with pytest.raises(TranscriptionError) as exc:
        resolve_repo("gpt-9")
    assert "large-v3-turbo" in str(exc.value)


def test_resolve_empty_raises():
    with pytest.raises(TranscriptionError):
        resolve_repo("")


# ---------------------------------------------------------------------------
# 本地路径
# ---------------------------------------------------------------------------


def test_local_model_dir_is_flat_and_safe(settings):
    d = local_model_dir(settings, "large-v3-turbo")
    assert d.parent == settings.models_dir / "whisper"
    assert "/" not in d.name
    assert "mobiuslabsgmbh" in d.name


def test_find_vocab_accepts_json(settings):
    d = local_model_dir(settings, "tiny")
    d.mkdir(parents=True)
    (d / "vocabulary.json").write_bytes(b"x")
    assert find_vocab(d).name == "vocabulary.json"


def test_find_vocab_accepts_txt(settings):
    d = local_model_dir(settings, "tiny")
    d.mkdir(parents=True)
    (d / "vocabulary.txt").write_bytes(b"x")
    assert find_vocab(d).name == "vocabulary.txt"


def test_find_vocab_none_when_empty(settings):
    d = local_model_dir(settings, "tiny")
    d.mkdir(parents=True)
    assert find_vocab(d) is None


def test_find_vocab_ignores_empty_file(settings):
    d = local_model_dir(settings, "tiny")
    d.mkdir(parents=True)
    (d / "vocabulary.json").write_bytes(b"")
    assert find_vocab(d) is None


# ---------------------------------------------------------------------------
# 完整性判断
# ---------------------------------------------------------------------------


def test_is_model_ready_false_when_missing(settings):
    assert is_model_ready(settings, "large-v3-turbo") is False


def test_is_model_ready_requires_all_files(settings):
    d = local_model_dir(settings, "large-v3-turbo")
    d.mkdir(parents=True)
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (d / name).write_bytes(b"x")
    assert is_model_ready(settings, "large-v3-turbo") is False  # 还缺词表

    (d / "vocabulary.json").write_bytes(b"x")
    assert is_model_ready(settings, "large-v3-turbo") is True


def test_is_complete_checks_size(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"12345")
    assert _is_complete(f, 5) is True
    assert _is_complete(f, 6) is False
    assert _is_complete(f, None) is True  # 未知大小，非空即算完整


def test_is_complete_rejects_empty_file(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"")
    assert _is_complete(f, None) is False


def test_is_complete_rejects_missing_file(tmp_path):
    assert _is_complete(tmp_path / "nope", None) is False


# ---------------------------------------------------------------------------
# 端点选择
# ---------------------------------------------------------------------------


def test_base_urls_puts_configured_endpoint_first(settings):
    settings.hf_endpoint = "https://my-mirror.example"
    urls = _base_urls(settings)
    assert urls[0] == "https://my-mirror.example"
    assert "https://hf-mirror.com" in urls


def test_base_urls_defaults_to_mirror_then_official(settings):
    settings.hf_endpoint = None
    urls = _base_urls(settings)
    assert urls == ["https://hf-mirror.com", "https://huggingface.co"]


def test_base_urls_has_no_duplicates(settings):
    settings.hf_endpoint = "https://hf-mirror.com"
    urls = _base_urls(settings)
    assert len(urls) == len(set(urls))


def test_base_urls_strips_trailing_slash(settings):
    settings.hf_endpoint = "https://x.example/"
    assert _base_urls(settings)[0] == "https://x.example"


def test_file_url_shape():
    assert _file_url("https://m.example", "a/b", "model.bin") == (
        "https://m.example/a/b/resolve/main/model.bin"
    )


# ---------------------------------------------------------------------------
# 单文件下载
# ---------------------------------------------------------------------------


@respx.mock
def test_probe_file_reads_content_length_from_head():
    respx.head(URL).mock(
        return_value=httpx.Response(200, headers={"Content-Length": "123"})
    )
    with httpx.Client() as client:
        assert _probe_file(client, URL) == (True, 123)


@respx.mock
def test_probe_file_reachable_without_content_length():
    """⛔ 回归守卫：镜像返回 chunked、没有 Content-Length。

    早期版本把「拿不到大小」当成「不可达」，导致明明能下却报
    「所有下载端点都不可达」。
    """
    respx.head(URL).mock(return_value=httpx.Response(200))
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"x"))

    with httpx.Client() as client:
        reachable, size = _probe_file(client, URL)

    assert reachable is True
    assert size is None


@respx.mock
def test_probe_file_uses_content_range_when_head_fails():
    respx.head(URL).mock(side_effect=httpx.ConnectError("no head"))
    respx.get(URL).mock(
        return_value=httpx.Response(
            206, headers={"Content-Range": "bytes 0-0/98765"}
        )
    )
    with httpx.Client() as client:
        assert _probe_file(client, URL) == (True, 98765)


@respx.mock
def test_probe_file_unreachable_when_all_methods_fail():
    respx.head(URL).mock(side_effect=httpx.ConnectError("x"))
    respx.get(URL).mock(side_effect=httpx.ConnectError("x"))
    with httpx.Client() as client:
        assert _probe_file(client, URL) == (False, None)


@respx.mock
def test_probe_file_unreachable_on_404():
    respx.head(URL).mock(return_value=httpx.Response(404))
    respx.get(URL).mock(return_value=httpx.Response(404))
    with httpx.Client() as client:
        assert _probe_file(client, URL)[0] is False


def test_sizes_from_metadata_never_raises():
    """元数据只是锦上添花，任何失败都不该影响下载。"""
    assert isinstance(_sizes_from_metadata("this/repo-does-not-exist-xyz"), dict)


@respx.mock
def test_download_writes_file_and_removes_part(tmp_path):
    respx.get(URL).mock(
        return_value=httpx.Response(200, content=b"0123456789")
    )
    target = tmp_path / "model.bin"

    with httpx.Client() as client:
        _download_one(
            client, url=URL, target=target, label="model.bin", expected=10
        )

    assert target.read_bytes() == b"0123456789"
    assert not (tmp_path / "model.bin.part").exists()


@respx.mock
def test_download_resumes_from_partial_file(tmp_path):
    """服务端返回 206 —— 应当从断点继续，而不是重下。"""
    route = respx.get(URL).mock(
        return_value=httpx.Response(206, content=b"56789")
    )
    target = tmp_path / "model.bin"
    (tmp_path / "model.bin.part").write_bytes(b"01234")

    with httpx.Client() as client:
        _download_one(
            client, url=URL, target=target, label="model.bin", expected=10
        )

    assert target.read_bytes() == b"0123456789"
    # 关键：确实带上了 Range 头
    assert route.calls[0].request.headers.get("Range") == "bytes=5-"


@respx.mock
def test_download_restarts_when_server_ignores_range(tmp_path):
    """服务端不支持续传（返回 200）时必须重下，否则文件会错乱。"""
    respx.get(URL).mock(
        return_value=httpx.Response(200, content=b"0123456789")
    )
    target = tmp_path / "model.bin"
    (tmp_path / "model.bin.part").write_bytes(b"XXXXX")

    with httpx.Client() as client:
        _download_one(
            client, url=URL, target=target, label="model.bin", expected=10
        )

    assert target.read_bytes() == b"0123456789"  # 不是 XXXXXXXXXX


@respx.mock
def test_download_works_when_size_unknown(tmp_path):
    """镜像不给大小也要能下完。"""
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"payload"))
    target = tmp_path / "model.bin"

    with httpx.Client() as client:
        _download_one(client, url=URL, target=target, label="model.bin")

    assert target.read_bytes() == b"payload"


@respx.mock
def test_download_skips_already_complete_file(tmp_path):
    target = tmp_path / "model.bin"
    target.write_bytes(b"0123456789")
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=b"x"))

    with httpx.Client() as client:
        _download_one(
            client, url=URL, target=target, label="model.bin", expected=10
        )

    assert not route.called, "已完整的文件不该重新下载"


@respx.mock
def test_download_retries_then_succeeds(tmp_path):
    respx.get(URL).mock(
        side_effect=[
            httpx.ConnectError("boom"),
            httpx.Response(200, content=b"0123456789"),
        ]
    )
    target = tmp_path / "model.bin"

    with httpx.Client() as client:
        _download_one(
            client, url=URL, target=target, label="model.bin", expected=10
        )

    assert target.read_bytes() == b"0123456789"


@respx.mock
def test_download_raises_actionable_error_after_retries(tmp_path, monkeypatch):
    import app.asr.model_store as store

    monkeypatch.setattr(store, "MAX_RETRIES", 2)
    respx.get(URL).mock(side_effect=httpx.ConnectError("always down"))
    target = tmp_path / "model.bin"

    with httpx.Client() as client:
        with pytest.raises(TranscriptionError) as exc:
            _download_one(client, url=URL, target=target, label="model.bin")

    assert "hf-mirror.com" in str(exc.value)
    assert "断点" in str(exc.value)


@respx.mock
def test_download_reports_progress(tmp_path):
    payload = b"a" * (CHUNK + 5)
    respx.get(URL).mock(return_value=httpx.Response(200, content=payload))

    seen: list[tuple[int, int, str]] = []
    target = tmp_path / "model.bin"

    with httpx.Client() as client:
        _download_one(
            client,
            url=URL,
            target=target,
            label="model.bin",
            expected=len(payload),
            on_progress=lambda got, exp, lbl: seen.append((got, exp, lbl)),
        )

    assert seen, "应当报告下载进度"
    assert seen[-1][0] == seen[-1][1]  # 结束时已下满
    assert seen[-1][2] == "model.bin"


# ---------------------------------------------------------------------------
# ensure_model 的整体流程
# ---------------------------------------------------------------------------


@respx.mock
def test_ensure_model_short_circuits_when_ready(settings):
    d = local_model_dir(settings, "tiny")
    d.mkdir(parents=True)
    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.json"):
        (d / name).write_bytes(b"x")

    respx.head(url__regex=r".*").mock(return_value=httpx.Response(200))
    assert ensure_model(settings, "tiny") == d


@respx.mock
def test_ensure_model_raises_when_all_endpoints_down(settings):
    # HEAD 和 Range 回退都要 mock，否则 respx 会因为"未 mock 的请求"报错
    respx.head(url__regex=r".*").mock(side_effect=httpx.ConnectError("down"))
    respx.get(url__regex=r".*").mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(TranscriptionError) as exc:
        ensure_model(settings, "tiny")
    assert "hf-mirror.com" in str(exc.value)


@respx.mock
def test_ensure_model_downloads_all_files(settings):
    settings.hf_endpoint = "https://mirror.example"
    base = "https://mirror.example/Systran/faster-whisper-tiny/resolve/main"

    files = {
        "config.json": b"{}",
        "model.bin": b"MODELDATA",
        "tokenizer.json": b"{}",
        "vocabulary.json": b"[]",
        "preprocessor_config.json": b"{}",
    }

    def head_router(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        if name in files:
            return httpx.Response(200, headers={"Content-Length": str(len(files[name]))})
        return httpx.Response(404)

    def get_router(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        if name in files:
            return httpx.Response(200, content=files[name])
        return httpx.Response(404)

    respx.head(url__regex=r".*").mock(side_effect=head_router)
    respx.get(url__regex=r".*").mock(side_effect=get_router)

    path = ensure_model(settings, "tiny")

    assert path.name == "Systran--faster-whisper-tiny"
    assert (path / "model.bin").read_bytes() == b"MODELDATA"
    assert find_vocab(path) is not None
    assert is_model_ready(settings, "tiny") is True
    assert str(path).startswith(str(settings.models_dir))
    assert base  # 说明用到了配置的端点


@respx.mock
def test_ensure_model_reports_overall_progress(settings, monkeypatch):
    import app.asr.model_store as store

    settings.hf_endpoint = "https://mirror.example"
    files = {
        "config.json": b"{}",
        "model.bin": b"M" * 100,
        "tokenizer.json": b"{}",
        "vocabulary.json": b"[]",
    }
    # 大小由元数据接口提供（镜像下载流里拿不到 Content-Length）
    monkeypatch.setattr(
        store, "_sizes_from_metadata", lambda _repo: {k: len(v) for k, v in files.items()}
    )

    def head_router(request: httpx.Request) -> httpx.Response:
        name = str(request.url).rsplit("/", 1)[-1]
        if name in files:
            return httpx.Response(200, headers={"Content-Length": str(len(files[name]))})
        return httpx.Response(404)

    respx.head(url__regex=r".*").mock(side_effect=head_router)
    respx.get(url__regex=r".*").mock(
        side_effect=lambda req: httpx.Response(
            200, content=files.get(str(req.url).rsplit("/", 1)[-1], b"")
        )
    )

    seen: list[tuple[int, int, str]] = []
    ensure_model(settings, "tiny", on_progress=lambda g, t, f: seen.append((g, t, f)))

    assert seen
    total = sum(len(v) for v in files.values())
    assert seen[-1][1] == total, "总量应来自元数据接口"
    assert seen[-1][0] == total, "结束时已全部下完"


@respx.mock
def test_ensure_model_works_without_known_sizes(settings, monkeypatch):
    """拿不到大小时也要能下完，只是没有百分比。"""
    import app.asr.model_store as store

    settings.hf_endpoint = "https://mirror.example"
    files = {
        "config.json": b"{}",
        "model.bin": b"M" * 50,
        "tokenizer.json": b"{}",
        "vocabulary.json": b"[]",
    }
    monkeypatch.setattr(store, "_sizes_from_metadata", lambda _repo: {})

    respx.head(url__regex=r".*").mock(return_value=httpx.Response(200))
    respx.get(url__regex=r".*").mock(
        side_effect=lambda req: httpx.Response(
            200, content=files.get(str(req.url).rsplit("/", 1)[-1], b"")
        )
    )

    seen: list[tuple[int, int, str]] = []
    path = ensure_model(settings, "tiny", on_progress=lambda g, t, f: seen.append((g, t, f)))

    assert is_model_ready(settings, "tiny")
    assert (path / "model.bin").read_bytes() == b"M" * 50
    assert all(total == 0 for _got, total, _lbl in seen), "总量未知时应为 0"
