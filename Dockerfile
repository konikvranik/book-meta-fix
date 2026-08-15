# Multi-arch image (linux/amd64, linux/arm64, linux/arm/v7).
# Build examples:
#   docker build -t bmf .
#   docker buildx build --platform linux/amd64,linux/arm64,linux/arm/v7 -t bmf:multi .
FROM python:3.12-slim AS base

# Optional external tools: poppler (pdftotext/pdfinfo) is the primary PDF
# path; calibre/pandoc/tesseract/OCR are too heavy for the image and stay
# optional on the host.
RUN apt-get update \
	&& apt-get install -y --no-install-recommends poppler-utils \
	&& rm -rf /var/lib/apt/lists/*

# PyPI publishes no linux/armv7l wheels for Pillow, rapidfuzz or PyYAML, so
# the arm/v7 leg compiles them from source: Pillow needs zlib (required) and
# libjpeg (covers are JPEGs) headers; rapidfuzz builds its C++ core only when
# cmake is present AND RAPIDFUZZ_BUILD_EXTENSION is set — otherwise it
# silently falls back to a much slower pure-Python wheel. Compile in a
# builder stage so the toolchain never reaches the runtime image.
FROM base AS build
RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		gcc g++ cmake ninja-build zlib1g-dev libjpeg62-turbo-dev \
	&& rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN RAPIDFUZZ_BUILD_EXTENSION=1 pip install --no-cache-dir --prefix=/install ".[pdf,llm]"

FROM base
COPY --from=build /install /usr/local

# Library + config are mounted at runtime:
#   docker run --rm -v "$PWD:/library" -v "$PWD/.env:/.env" bmf report --library /library
VOLUME ["/library"]
ENV BMF_LIBRARY=/library

WORKDIR /library
ENTRYPOINT ["bmf"]
CMD ["--help"]
