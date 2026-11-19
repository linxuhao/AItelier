# aitelier-embed:local — llama.cpp's llama-server (CPU build) serving bge-m3
# embeddings for the steward's memory search (README.md; design §3.8.2).
#
# The build context is ~/.local/share/aitelier-embed (it holds the release tarball in
# dl/); this directory comes in as the named context `embed-server`, for llama.sha256:
#
#   docker build -t aitelier-embed:local \
#     -f integrations/embed-server/Dockerfile \
#     --build-context embed-server=integrations/embed-server \
#     ~/.local/share/aitelier-embed
#
# Nothing is downloaded here except libgomp1 from Ubuntu's apt sources (owner-approved,
# 2026-11-19). The model is not in the image: run.sh mounts it read-only.

# Verify the release tarball against llama.sha256 and unpack it, with no network.
FROM ubuntu:24.04@sha256:73b999393d07f2bf65a40373778a44dc9a6d54ad7718d29247d8ff9413c4d6a8 AS unpack
WORKDIR /tmp/dl
COPY --from=embed-server llama.sha256 ./
COPY dl/llama-b12119-bin-ubuntu-x64.tar.gz ./
RUN --network=none sha256sum -c llama.sha256 \
    && tar -xzf llama-b12119-bin-ubuntu-x64.tar.gz -C /opt \
    && mv /opt/llama-b12119 /opt/llama

FROM ubuntu:24.04@sha256:73b999393d07f2bf65a40373778a44dc9a6d54ad7718d29247d8ff9413c4d6a8
# The release is built with OpenMP; the bare image has no libgomp.so.1.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
# The whole release directory as shipped: the CPU backend is picked per
# microarchitecture at run time from libggml-cpu-*.so in the same directory.
COPY --from=unpack /opt/llama /opt/llama
ENV PATH=/opt/llama:$PATH
# Every binary and library resolves its libraries (the CPU backends are dlopen'ed, so
# ldd them too).
RUN --network=none for f in /opt/llama/llama-server /opt/llama/*.so*; do \
      if ldd "$f" | grep 'not found'; then echo "missing library for $f" >&2; exit 1; fi; \
    done \
    && ldd /opt/llama/llama-server
# uid 1000 (the image's own `ubuntu` user), not root.
USER 1000:1000
# Smoke, with no network: the binary runs and has --offline.
RUN --network=none llama-server --version && llama-server --help | grep -q -- --offline
EXPOSE 8080
ENTRYPOINT ["/opt/llama/llama-server"]
