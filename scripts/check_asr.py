"""快速自检：CTranslate2 / CUDA 在当前机器上到底能不能跑。

专门为 Blackwell（sm_120）这类新卡准备 —— 光看驱动在不在没用，
必须问 CTranslate2 自己「你支持哪些 compute_type」。

用法::

    .\\.venv\\Scripts\\python.exe -m scripts.check_asr
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许直接以脚本方式运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.asr.whisper_engine import (  # noqa: E402
    setup_cuda_dll_paths,
    supported_compute_types,
)
from app.console import enable_utf8_output  # noqa: E402


def main() -> int:
    enable_utf8_output()
    print("=" * 68)
    print("CTranslate2 / CUDA 自检")
    print("=" * 68)

    try:
        import ctranslate2
    except ImportError:
        print("❌ 未安装 ctranslate2")
        return 1

    print(f"ctranslate2 版本 : {ctranslate2.__version__}")
    if ctranslate2.__version__ < "4.6.3":
        print(
            "⚠️  版本低于 4.6.3：这是首个用 CUDA 12.8 构建的版本，\n"
            "    更早的版本在 Blackwell(sm_120) 上只能靠 PTX JIT，风险较高。"
        )

    added = setup_cuda_dll_paths()
    print(f"已注册 CUDA DLL 目录 : {len(added)} 个")
    for d in added:
        print(f"    {d}")

    print("-" * 68)
    try:
        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 查询 CUDA 设备失败：{exc}")
        return 1

    print(f"检测到的 CUDA 设备数 : {count}")
    if count <= 0:
        print("❌ 没有可用 CUDA 设备，转写将走 CPU。")
        return 1

    print("-" * 68)
    types = supported_compute_types("cuda")
    print(f"CUDA 支持的 compute_type : {sorted(types) or '（空）'}")

    if "float16" in types:
        print("\n✅ 结论：可以用 GPU 转写（compute_type=float16）")
        print("   —— 注意永远不要用 'auto'：它会选 int8 而在 Blackwell 上崩溃。")
        return 0

    print("\n❌ 结论：CUDA 可用但不支持 float16，请降级 CPU 转写。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
