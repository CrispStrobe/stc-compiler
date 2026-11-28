# stc-compiler as a container — the Hugging Face Space mirror of the
# Vercel deployment (cstr/stc-compiler; scripts/deploy-hf.sh uploads it).
#
# The toolchains are the VENDORED ones from this repo (sdcc in bin/+share/,
# avr-gcc in avr/, arm-none-eabi in arm/, cc65 in cc65/) — x86_64 Linux
# builds targeting glibc 2.34, which run fine on bookworm's 2.36. app.py
# stages them into /tmp at cold start exactly as it does on Vercel, so the
# final container needs no apt toolchain packages at all. Cowgol is built in
# a separate older-glibc stage so its compiler tools also travel to Vercel.
FROM python:3.11-slim-bullseye AS cowgol-build
COPY scripts/cowgol-bullseye.list /etc/apt/sources.list
RUN rm -f /etc/apt/sources.list.d/debian.sources
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git ninja-build lua5.4 bison flex gpp moreutils nasm \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY scripts/build-cowgol-bundle.py ./scripts/build-cowgol-bundle.py
RUN python3 scripts/build-cowgol-bundle.py --output /cowgol-bundle

FROM python:3.11-slim-bookworm

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=cowgol-build /cowgol-bundle ./vendor/cowgol

# HF Spaces run as uid 1000 with a read-only-ish HOME elsewhere; app.py
# only writes under /tmp, which is writable for everyone.
RUN useradd -m -u 1000 user
USER user

EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
