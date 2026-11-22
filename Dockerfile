# linux/amd64; the digest is the image the commits were labeled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
ENV SQLGLOT_ROOT=/opt/sqlglot \
    SKIP_INTEGRATION=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_MAX_THREADS=1

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --disable-pip-version-check -r /tmp/requirements.lock

# sqlglot at the pinned commit, from its own repository, with bytecode compiled
RUN python -c "import urllib.request, tarfile, io, os; \
data = urllib.request.urlopen('https://codeload.github.com/tobymao/sqlglot/tar.gz/${SQLGLOT_SHA}', timeout=300).read(); \
tarfile.open(fileobj=io.BytesIO(data)).extractall('/opt', filter='tar'); \
os.rename('/opt/sqlglot-${SQLGLOT_SHA}', '/opt/sqlglot')" \
 && PYTHONDONTWRITEBYTECODE= python -m compileall -q -j 4 /opt/sqlglot/sqlglot /opt/sqlglot/tests \
 && chmod -R a+rX /opt/sqlglot

COPY selector.py analyze.py runner.py datadiff.py narrow.py costs.json map.bin probes.bin casemap.bin callmap.bin keymap.bin cmpmap.bin small.bin /app/
RUN chmod -R a+rX /app && PYTHONDONTWRITEBYTECODE= python -m compileall -q /app
# the executor tests' tables, built once from the pinned commit (the run falls back to building them)
RUN cd /app && python -c "import runner; runner.save_exec_cache()" ; chmod a+r /app/execcache.pickle || true
RUN cd /app && python -c "import runner; runner.save_duck_cache()" ; chmod a+r /app/duckcache.pickle || true
RUN cd /app && python -c "import runner; runner.save_refs()" ; chmod a+r /app/refs.pickle || true
RUN cd /app && python -c "import narrow; narrow.Source('/opt/sqlglot').save_index(narrow.INDEX_FILE)" ; chmod a+r /app/narrow.idx || true

ENV BR_BATCH=1
WORKDIR /app
CMD ["python", "/app/selector.py"]
