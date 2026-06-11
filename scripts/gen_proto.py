#!/usr/bin/env python3
"""Regenerate gRPC stubs from src/loockit/api/proto/sesame.proto.

Run after editing the .proto:  python scripts/gen_proto.py
Requires the `dev` extra (grpcio-tools). The generated *_pb2*.py files are
committed so the package builds without protoc.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PROTO_DIR = SRC / "loockit" / "api" / "proto"
OUT_DIR = SRC / "loockit" / "api"


def main() -> int:
    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{PROTO_DIR}",
        f"--python_out={OUT_DIR}",
        f"--grpc_python_out={OUT_DIR}",
        str(PROTO_DIR / "sesame.proto"),
    ]
    subprocess.run(cmd, check=True)

    # protoc emits a flat `import sesame_pb2`; rewrite to package-relative.
    grpc_file = OUT_DIR / "sesame_pb2_grpc.py"
    text = grpc_file.read_text()
    text = re.sub(
        r"^import sesame_pb2 as sesame__pb2$",
        "from loockit.api import sesame_pb2 as sesame__pb2",
        text,
        flags=re.MULTILINE,
    )
    grpc_file.write_text(text)
    print("generated:", OUT_DIR / "sesame_pb2.py", grpc_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
