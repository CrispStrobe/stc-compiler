# stc-compiler as a container — the Hugging Face Space mirror of the
# Vercel deployment (cstr/stc-compiler; scripts/deploy-hf.sh uploads it).
#
# The toolchains are the VENDORED ones from this repo (sdcc in bin/+share/,
# avr-gcc in avr/, arm-none-eabi in arm/, cc65 in cc65/) — x86_64 Linux
# builds targeting glibc 2.34, which run fine on bookworm's 2.36. app.py
# stages them into /tmp at cold start exactly as it does on Vercel, so the
# container needs no apt toolchain packages at all.
FROM python:3.11-slim-bookworm

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# HF Spaces run as uid 1000 with a read-only-ish HOME elsewhere; app.py
# only writes under /tmp, which is writable for everyone.
RUN useradd -m -u 1000 user
USER user

EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
