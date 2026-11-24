# Build config for quant/utils/cpp_native/vq_kernels (nanobind).
#
# ADDITIVE to the pure-Python indicators: compiles a nanobind extension exposing
# the sequential recurrence kernels (adaptive_z, kalman, fisher) at
# quant/utils/cpp_native/. The Python callers import it lazily with a fallback to
# the pure-Python loop when the compiled module is unavailable.
#
# Note: this newest nanobind wheel drops the `nanobind.build` shim, so we wire
# setuptools directly and compile nanobind's own core .cpp translation units.
#
# The extension is declared under the fully-qualified name
# `quant.utils.cpp_native.vq_kernels`, so setuptools resolves the in-place `.so`
# copy destination (`quant/utils/cpp_native/`) relative to the repository root
# (the directory that contains `quant/`). The build is therefore run from there
# and every source/include path below must be resolved against *this file's*
# directory or nanobind's absolute include dir — never against cwd.
import sys
from pathlib import Path

import nanobind as nb
from setuptools import Extension, setup

_SRC_DIR = Path(__file__).resolve().parent
_INC = Path(nb.include_dir())
_core = sorted(
    p
    for p in (_INC.parent / "src").glob("*.cpp")
    if "nb_backend.cpp" not in p.name and "nb_combined.cpp" not in p.name
)
_robin = _INC.parent / "ext" / "robin_map" / "include"

_cfg = [
    "-std=c++17",
    "-O3",
    "-fvisibility=hidden",
    "-fno-strict-aliasing",
    "-D_GLIBCXX_ASSERTIONS",
] + (["-stdlib=libc++", "-fexceptions"] if sys.platform == "darwin" else [])

ext = Extension(
    "quant.utils.cpp_native.vq_kernels",
    sources=[str(_SRC_DIR / "vq_kernels.cpp"), *map(str, _core)],
    include_dirs=[str(_INC), str(_robin), str(_SRC_DIR)],
    language="c++",
    extra_compile_args=_cfg,
    extra_link_args=["-stdlib=libc++"] if sys.platform == "darwin" else [],
)

setup(
    name="quant-native-kernels",
    version="0.1.0",
    packages=[],
    ext_modules=[ext],
)
