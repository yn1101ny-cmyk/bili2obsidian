"""画面识别（抽帧 / 去重 / OCR / 清洗）测试。

重点验证三件容易静默出错的事：

1. **时间戳准确** —— ``fps=1/N`` 抽帧会系统性错半个间隔，必须逐点定位；
2. **去重不能误杀** —— dHash 会把「新出现一行文字」判成重复，必须用分块 MAD；
3. **字幕剔除** —— 与我们已有的音频转写比对，这是本项目的独有优势。

需要 ffmpeg 的用例在缺失时会跳过，不联网。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from app.vision.dedup import (
    block_distance,
    block_signature,
    changed_block_count,
    dedup_frames,
)
from app.vision.frames import (
    _DURATION_RE,
    Frame,
    extract_frame,
    extract_frames,
    plan_timestamps,
    probe_duration,
)
from app.vision.ocr import OcrEngine, is_available, suggested_threads
from app.vision.pipeline import (
    OnScreenText,
    drop_persistent,
    drop_subtitles,
    find_persistent_lines,
    is_subtitle_like,
    merge_consecutive,
    normalize,
)


# ===========================================================================
# 抽帧：时间戳规划
# ===========================================================================


def test_plan_timestamps_basic():
    stamps = plan_timestamps(60.0, interval=15.0)
    assert stamps == [0.0, 15.0, 30.0, 45.0]


def test_plan_timestamps_never_exceeds_duration():
    stamps = plan_timestamps(10.0, interval=15.0)
    assert stamps == [0.0]
    assert all(t < 10.0 for t in stamps)


def test_plan_timestamps_widens_interval_for_long_videos():
    """一小时的视频不该抽出几百帧。"""
    stamps = plan_timestamps(3600.0, interval=15.0, max_frames=120)
    assert len(stamps) <= 120
    # 间隔被自动放宽，但仍然覆盖整个视频
    assert stamps[-1] > 3600 * 0.9


def test_plan_timestamps_handles_unknown_duration():
    assert plan_timestamps(0.0) == [0.0]
    assert plan_timestamps(-1.0) == [0.0]


def test_plan_timestamps_strictly_increasing():
    stamps = plan_timestamps(600.0, interval=15.0)
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


# ===========================================================================
# 抽帧：时长解析（imageio-ffmpeg 不带 ffprobe，只能解析 stderr）
# ===========================================================================


@pytest.mark.parametrize(
    "line,expected",
    [
        ("  Duration: 00:24:55.12, start: 0.000000, bitrate: 1234 kb/s", 1495.12),
        ("  Duration: 00:00:10.00, start: 0.000000", 10.0),
        ("  Duration: 01:02:03.50, start:", 3723.5),
    ],
)
def test_duration_regex(line, expected):
    match = _DURATION_RE.search(line)
    assert match is not None
    h, m, s = match.groups()
    assert int(h) * 3600 + int(m) * 60 + float(s) == pytest.approx(expected)


def test_duration_regex_no_match():
    assert _DURATION_RE.search("no duration here") is None


# ===========================================================================
# 抽帧：真的跑 ffmpeg
# ===========================================================================


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory) -> Path:
    """造一个 3 秒的测试视频（每秒钟内容不同，便于验证时间戳）。"""
    from app.media.ffmpeg import find_ffmpeg

    exe = find_ffmpeg()
    if not exe:
        pytest.skip("没有 ffmpeg")

    out = tmp_path_factory.mktemp("video") / "sample.mp4"
    # testsrc 自带变化的图案与秒计数，适合验证抽到的是哪一帧
    cmd = [
        exe, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
        "-pix_fmt", "yuv420p", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0 or not out.exists():
        pytest.skip(f"生成测试视频失败：{proc.stderr[:200]!r}")
    return out


def test_probe_duration(sample_video):
    duration = probe_duration(sample_video)
    assert duration == pytest.approx(3.0, abs=0.2)


def test_extract_frame_writes_file(sample_video, tmp_path):
    target = tmp_path / "f.jpg"
    assert extract_frame(sample_video, 1.0, target) is True
    assert target.exists() and target.stat().st_size > 0


def test_extract_frame_at_different_times_differs(sample_video, tmp_path):
    """⛔ 回归守卫：不同时间点必须抽到**不同**的画面。

    如果时间戳算错（比如统一落到 0），这里会抓到两张一样的图。
    """
    import cv2

    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    assert extract_frame(sample_video, 0.3, a) is True
    assert extract_frame(sample_video, 2.5, b) is True

    img_a = cv2.imread(str(a), cv2.IMREAD_GRAYSCALE)
    img_b = cv2.imread(str(b), cv2.IMREAD_GRAYSCALE)
    assert img_a is not None and img_b is not None
    assert block_distance(block_signature(img_a), block_signature(img_b)) > 3.0


def test_extract_frames_reports_correct_timestamps(sample_video, tmp_path):
    """帧对象上的时间戳必须就是我们请求的那些值。"""
    frames = extract_frames(
        sample_video, interval=1.0, max_frames=10, work_dir=tmp_path / "frames"
    )
    assert len(frames) >= 2
    for frame in frames:
        assert isinstance(frame, Frame)
        assert isinstance(frame.timestamp, float)
    stamps = [f.timestamp for f in frames]
    assert stamps == sorted(stamps)
    assert stamps[0] == pytest.approx(0.0)


def test_extract_frames_missing_file_raises(tmp_path):
    from app.errors import MyNoteError

    with pytest.raises(MyNoteError):
        extract_frames(tmp_path / "nope.mp4", work_dir=tmp_path / "f")


def test_frame_clock(sample_video, tmp_path):
    frames = extract_frames(
        sample_video, interval=1.0, max_frames=5, work_dir=tmp_path / "f"
    )
    assert frames[0].clock() == "00:00"


# ===========================================================================
# 去重
# ===========================================================================


def _solid(value: int, size: int = 64) -> np.ndarray:
    return np.full((size, size), value, dtype=np.uint8)


def test_block_signature_shape():
    assert block_signature(_solid(100), grid=8).shape == (8, 8)


def test_block_signature_accepts_color():
    img = np.zeros((32, 32, 3), dtype=np.uint8)
    img[..., 0] = 255
    assert block_signature(img).shape == (8, 8)


def test_block_signature_handles_tiny_image():
    assert block_signature(_solid(10, size=4)).shape[0] >= 2


def test_block_signature_empty_image():
    assert block_signature(np.zeros((0, 0), dtype=np.uint8)).shape == (8, 8)


def test_block_distance_zero_for_identical():
    sig = block_signature(_solid(120))
    assert block_distance(sig, sig) == 0.0


def test_block_distance_positive_for_different():
    a = block_signature(_solid(50))
    b = block_signature(_solid(200))
    assert block_distance(a, b) > 100


def test_block_distance_mismatched_shapes_is_infinite():
    assert block_distance(
        np.zeros((8, 8), dtype=np.float32), np.zeros((4, 4), dtype=np.float32)
    ) == float("inf")


def test_changed_block_count_counts_local_change():
    """⛔ 核心：**局部新增内容**必须被识别出来。

    这是 dHash 做不到的事（实测把「新出现一行文字」判成 3、
    把「翻页」判成 5，都会低于阈值被当成重复帧丢掉）。
    """
    base = _solid(80)
    # 模拟「画面下方多了一行文字」：只改底部一小条
    changed = base.copy()
    changed[52:60, 8:56] = 240

    a = block_signature(base)
    b = block_signature(changed)

    assert changed_block_count(a, b) >= 3, "局部新增内容必须被检测到"
    # 整体平均差反而不大 —— 这正是只看平均值会漏掉的原因
    assert block_distance(a, b) < 40


def test_changed_block_count_zero_for_identical():
    sig = block_signature(_solid(100))
    assert changed_block_count(sig, sig) == 0


def test_dedup_keeps_first_frame():
    frames = ["a", "a", "a"]
    kept = dedup_frames(frames, load=lambda f: _solid(80))
    assert kept == ["a"]


def test_dedup_keeps_changed_frames():
    images = {
        "a": _solid(80),
        "b": _solid(200),
    }
    kept = dedup_frames(["a", "b", "b"], load=lambda f: images[f])
    assert kept == ["a", "b"]


def test_dedup_keeps_frames_that_fail_to_load():
    """读不出来的帧一律保留 —— 宁可多花一点 OCR 时间，也不要漏内容。"""
    kept = dedup_frames(["a", "broken"], load=lambda f: None)
    assert "broken" in kept


def test_dedup_preserves_local_change():
    """⛔ 回归守卫：只有一小条变化的帧不能被误杀。"""
    base = _solid(80)
    changed = base.copy()
    changed[52:60, 8:56] = 240
    images = {"a": base, "b": changed}
    kept = dedup_frames(["a", "b"], load=lambda f: images[f])
    assert kept == ["a", "b"]


# ===========================================================================
# OCR 引擎
# ===========================================================================


def test_ocr_availability_reports_cleanly():
    ok, detail = is_available()
    assert isinstance(ok, bool)
    assert detail


def test_suggested_threads_is_not_max_cores():
    """⛔ 回归守卫：不能用满逻辑核 —— 实测那样会慢 3.5 倍。"""
    import os

    import app.vision.ocr as ocr

    original = os.cpu_count
    try:
        ocr.os.cpu_count = lambda: 24  # type: ignore[assignment]
        assert suggested_threads() == 12
        ocr.os.cpu_count = lambda: 8  # type: ignore[assignment]
        assert suggested_threads() == 4
        ocr.os.cpu_count = lambda: 4  # type: ignore[assignment]
        assert suggested_threads() == 4
    finally:
        ocr.os.cpu_count = original  # type: ignore[assignment]


def test_ocr_engine_does_not_load_model_on_construct():
    engine = OcrEngine(threads=4)
    assert engine.threads == 4
    assert engine._engine is None


# ===========================================================================
# 清洗：字幕剔除（本项目的独有优势）
# ===========================================================================


def test_normalize_strips_punctuation_and_whitespace():
    assert normalize("你好，世界！ ABC") == "你好世界abc"
    assert normalize("a-b_c") == "abc"


def test_normalize_empty():
    assert normalize("") == ""
    assert normalize(None) == ""  # type: ignore[arg-type]


def test_short_lines_are_dropped_as_noise():
    assert is_subtitle_like("ab", "") is True
    assert is_subtitle_like("", "任意转写") is True


def test_subtitle_detected_by_substring():
    transcript = "大家好今天我们聊一聊向量数据库它不是银弹"
    assert is_subtitle_like("今天我们聊一聊向量数据库", transcript) is True


def test_subtitle_detected_with_punctuation_difference():
    """字幕带标点、转写不带，归一化之后应当命中。"""
    transcript = "大家好今天我们聊一聊向量数据库"
    assert is_subtitle_like("大家好，今天我们聊一聊向量数据库。", transcript) is True


def test_non_subtitle_survives():
    """画面上独有的内容（比如提示词）不能被当成字幕删掉。"""
    transcript = "大家好今天我们聊一聊向量数据库它不是银弹"
    assert is_subtitle_like("你是一个专业的助手", transcript) is False


def test_subtitle_detection_without_transcript_keeps_everything():
    assert is_subtitle_like("任意一行内容", "") is False


def test_drop_subtitles_keeps_screen_only_content():
    frames = [
        ["大家好今天我们聊一聊", "你是一个专业的助手"],
        ["向量数据库不是银弹", "请按以下格式输出"],
    ]
    transcript = "大家好今天我们聊一聊向量数据库不是银弹"
    result = drop_subtitles(frames, transcript)
    assert result[0] == ["你是一个专业的助手"]
    assert result[1] == ["请按以下格式输出"]


def test_drop_subtitles_noop_without_transcript():
    frames = [["a", "b"]]
    assert drop_subtitles(frames, "") == frames


# ===========================================================================
# 清洗：固定水印
# ===========================================================================


def test_persistent_lines_detected():
    frames = [["某UP主", f"内容{i}"] for i in range(10)]
    persistent = find_persistent_lines(frames)
    assert normalize("某UP主") in persistent
    assert len(persistent) == 1


def test_persistent_lines_need_enough_frames():
    """⛔ 护栏：帧数太少时不敢判定，否则会把正文全删了。"""
    frames = [["正文内容"] for _ in range(3)]
    assert find_persistent_lines(frames, min_frames=6) == set()


def test_persistent_lines_partial_occurrence_not_dropped():
    frames = [[f"独特内容{i}"] for i in range(10)]
    assert find_persistent_lines(frames) == set()


def test_drop_persistent_removes_watermark():
    frames = [["某UP主", "正文一"], ["某UP主", "正文二"]]
    persistent = {normalize("某UP主")}
    assert drop_persistent(frames, persistent) == [["正文一"], ["正文二"]]


def test_drop_persistent_empty_set_is_noop():
    frames = [["a"]]
    assert drop_persistent(frames, set()) == frames


# ===========================================================================
# 合并与渲染
# ===========================================================================


def _frame(ts: float, name: str) -> Frame:
    return Frame(timestamp=ts, path=Path(f"{name}.jpg"))


def test_merge_consecutive_collapses_identical():
    frames = [_frame(0, "a"), _frame(15, "b"), _frame(30, "c")]
    lines = [["同样的内容"], ["同样的内容"], ["新内容"]]
    merged = merge_consecutive(frames, lines)
    assert len(merged) == 2
    assert merged[0].timestamp == 0.0
    assert merged[1].timestamp == 30.0


def test_merge_consecutive_keeps_first_occurrence_timestamp():
    """时间戳取首次出现的时刻，跳转才落到内容刚出现的位置。"""
    frames = [_frame(0, "a"), _frame(15, "b"), _frame(30, "c")]
    lines = [["x"], ["x"], ["x"]]
    merged = merge_consecutive(frames, lines)
    assert len(merged) == 1
    assert merged[0].timestamp == 0.0


def test_merge_consecutive_skips_empty_frames():
    frames = [_frame(0, "a"), _frame(15, "b"), _frame(30, "c")]
    lines = [["内容"], [], ["另一段"]]
    merged = merge_consecutive(frames, lines)
    assert [m.timestamp for m in merged] == [0.0, 30.0]


def test_onscreen_render_has_timestamp_and_lines():
    item = OnScreenText(timestamp=143.0, lines=["第一行", "第二行"])
    text = item.render()
    assert "[02:23]" in text
    assert "第一行" in text
    assert "画面内容" in text


def test_onscreen_render_truncates_many_lines():
    item = OnScreenText(timestamp=0.0, lines=[f"行{i}" for i in range(30)])
    text = item.render(max_lines=5)
    assert "还有 25 行" in text


def test_onscreen_clock():
    assert OnScreenText(timestamp=3723.0, lines=[]).clock() == "1:02:03"


# ===========================================================================
# 缓存往返（曾经的 NameError: json 没导入就在这里炸的）
# ===========================================================================


def test_vision_cache_round_trip(settings):
    from app.pipeline import NotePipeline

    pipeline = NotePipeline(settings)
    path = pipeline._vision_cache_path("BV1TEST", 2)
    blocks = [(0.0, "[00:00] 画面内容：\n  git init"), (143.5, "另一段")]

    pipeline._write_vision_cache(path, blocks)
    assert path.exists()

    loaded = pipeline._read_vision_cache(path)
    assert loaded == blocks
    path.unlink()


def test_vision_cache_missing_returns_none(settings):
    from app.pipeline import NotePipeline

    pipeline = NotePipeline(settings)
    assert pipeline._read_vision_cache(
        settings.cache_dir / "vision" / "nope.json"
    ) is None


def test_vision_cache_corrupt_returns_none(settings):
    from app.pipeline import NotePipeline

    pipeline = NotePipeline(settings)
    path = settings.cache_dir / "vision" / "broken.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("这不是 JSON", encoding="utf-8")
    assert pipeline._read_vision_cache(path) is None


def test_vision_cache_empty_list_is_none(settings):
    """空结果不当作有效缓存 —— 否则一次失败会被永久缓存下来。"""
    from app.pipeline import NotePipeline

    pipeline = NotePipeline(settings)
    path = settings.cache_dir / "vision" / "empty.json"
    pipeline._write_vision_cache(path, [])
    assert pipeline._read_vision_cache(path) is None
