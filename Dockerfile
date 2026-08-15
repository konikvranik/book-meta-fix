# Multi-arch image (linux/amd64, linux/arm64, linux/arm/v7).
# Build examples:
#   docker build -t bmf .
#   docker buildx build --platform linux/amd64,linux/arm64,linux/arm/v7 -t bmf:multi .
FROM python:3.12-slim AS base

# Optional external tools: poppler (pdftotext/pdfinfo) is the primary PDF
# path; calibre/pandoc/tesseract/OCR are too heavy for the image and stay
# optional on the host. libyaml-0-2 is the runtime lib for the source-built
# yaml._yaml on arm/v7 (PyPI wheels bundle it; our compiled one links it).
RUN apt-get update \
	&& apt-get install -y --no-install-recommends poppler-utils libyaml-0-2 \
	&& rm -rf /var/lib/apt/lists/*

# PyPI publishes no linux/armv7l wheels for Pillow, rapidfuzz or PyYAML, so
# the arm/v7 leg compiles them from source. Pillow needs zlib (hard
# requirement) and libjpeg (covers are JPEGs) headers, plus libc6-dev —
# trixie's gcc only *recommends* it, so --no-install-recommends skips the
# C library headers. rapidfuzz builds its C++ core only when cmake is
# present AND RAPIDFUZZ_BUILD_EXTENSION is set, PyYAML its libyaml binding
# only with PYYAML_FORCE_LIBYAML — both otherwise silently fall back to a
# much slower pure-Python wheel instead of failing the build. Compile in a
# builder stage so the toolchain never reaches the runtime image.
FROM base AS build
RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		gcc g++ cmake ninja-build libc6-dev zlib1g-dev libjpeg62-turbo-dev libyaml-dev \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN RAPIDFUZZ_BUILD_EXTENSION=1 PYYAML_FORCE_LIBYAML=1 pip install --no-cache-dir --prefix=/install ".[pdf,llm]"

FROM base
COPY --from=build /install /usr/local

# Library + config are mounted at runtime:
#   docker run --rm -v "$PWD:/library" -v "$PWD/.env:/.env" bmf report --library /library
VOLUME ["/library"]
ENV BMF_LIBRARY=/library

WORKDIR /library
ENTRYPOINT ["bmf"]
CMD ["--help"]
