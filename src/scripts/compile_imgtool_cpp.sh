#!/bin/bash

set -euo pipefail

output_dir="data/imgtool"
output_path="${output_dir}/imgtool-cpp"
temporary_path="${output_path}.tmp.$$"

mkdir -p "$output_dir"
trap 'rm -f "$temporary_path"' EXIT

# 先生成临时文件，编译完全成功后再原子替换，避免失败时破坏旧版可执行文件。
g++ src/scripts/imgtool.cpp -o "$temporary_path" --std=c++17 -O2
chmod +x "$temporary_path"
mv -f "$temporary_path" "$output_path"

trap - EXIT
